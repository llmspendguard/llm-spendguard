"""Guard — the dispatch governor's REALTIME RESERVE: a batch fan can never starve a realtime call of a governor slot.
Tests the _Bucket primitive DIRECTLY (rpm=0 → no pacing, deterministic; no Governor/config/adapters/network):
  · reserve>0: batch admissions (sla_class="batch") are capped at (limit-reserve), so once batch fills its sub-limit a
    further batch acquire TIMES OUT — but a REALTIME acquire (sla_class=None) still gets the reserved slot;
  · the main limit is still enforced (all slots full → even realtime times out);
  · release is symmetric on sla_class (a batch release frees BOTH the main slot and the batch sub-slot), so both
    semaphores drain back and both classes admit again — no permit leak;
  · reserve=0 (the DEFAULT): no batch sub-semaphore at all, batch uses the FULL limit — byte-identical to today;
  · reserve is capped at limit-1: a 1-slot bucket can't reserve (batch would deadlock forever otherwise).
No deadlock: every acquire runs under a finite deadline, so a lock-order bug would surface as a DispatchTimeout or a
suite-budget overrun, never a silent hang. Lock order is fixed (batch: sub → main; realtime: main only)."""
import os
import sys
import tempfile

os.environ.setdefault("SPENDGUARD_TEST_ISOLATED", "1")
os.environ.setdefault("SPENDGUARD_HOME", tempfile.mkdtemp(prefix="spendguard-reserve-"))

from spendguard import dispatch

_fails = []
def ck(label, cond):
    if not cond:
        _fails.append(label)
    print(f"  [{'OK' if cond else 'FAIL'}] {label}")

print("-- reserve=1 of limit=3: batch capped at 2, the reserved slot stays for realtime --")
b = dispatch._Bucket("test:lane", limit=3, rpm=0, reserve=1)
ck("reserve stored, batch sub-semaphore built", b.reserve == 1 and b._sem_batch is not None)
w1 = b.acquire(1.0, sla_class="batch")
w2 = b.acquire(1.0, sla_class="batch")            # batch sub-limit (2) now full
ck("two batch admissions succeed", isinstance(w1, float) and isinstance(w2, float) and b.in_flight == 2)
try:
    b.acquire(0.05, sla_class="batch")
    ck("a 3rd batch admission is blocked by the sub-limit", False)
except dispatch.DispatchTimeout:
    ck("a 3rd batch admission is blocked by the sub-limit (reserved slot withheld)", True)
wr = b.acquire(0.5, sla_class=None)               # realtime takes the reserved 3rd slot
ck("realtime is still admitted into the reserved slot", isinstance(wr, float) and b.in_flight == 3)
try:
    b.acquire(0.05, sla_class=None)
    ck("the main limit is still enforced (all 3 full)", False)
except dispatch.DispatchTimeout:
    ck("the main limit is still enforced (all 3 full → even realtime waits)", True)

print("-- release is symmetric: a batch release frees the sub-slot too, so batch admits again --")
b.release(sla_class="batch")                       # in_flight 3→2, one batch sub-slot freed
w3 = b.acquire(0.5, sla_class="batch")
ck("after a batch release, a batch admission succeeds again", isinstance(w3, float) and b.in_flight == 3)

print("-- full drain returns both semaphores to baseline (no permit leak) --")
b.release(sla_class="batch")
b.release(sla_class="batch")
b.release(sla_class=None)
ck("in_flight drains to 0", b.in_flight == 0)
wb = b.acquire(0.2, sla_class="batch")
wrr = b.acquire(0.2, sla_class=None)
ck("after full drain both classes admit again (semaphores balanced)", isinstance(wb, float) and isinstance(wrr, float))
b.release(sla_class="batch")
b.release(sla_class=None)

print("-- reserve=0 (DEFAULT): no sub-semaphore, batch uses the FULL limit (byte-identical to today) --")
b0 = dispatch._Bucket("test:none", limit=2, rpm=0, reserve=0)
ck("reserve=0 → no batch sub-semaphore", b0.reserve == 0 and b0._sem_batch is None)
b0.acquire(0.5, sla_class="batch")
b0.acquire(0.5, sla_class="batch")                 # both slots filled by BATCH — nothing withheld
try:
    b0.acquire(0.05, sla_class="batch")
    ck("reserve=0: batch fills the whole limit", False)
except dispatch.DispatchTimeout:
    ck("reserve=0: batch uses the FULL limit (no slot withheld)", True)
b0.release(sla_class="batch")
b0.release(sla_class="batch")

print("-- reserve capped at limit-1: a 1-slot bucket cannot reserve (batch would deadlock) --")
b1 = dispatch._Bucket("test:one", limit=1, rpm=0, reserve=5)
ck("reserve capped to 0 on a 1-slot bucket", b1.reserve == 0 and b1._sem_batch is None)

print(f"\n{'[FAIL]' if _fails else 'OK'} test_dispatch_realtime_reserve: {len(_fails)} failure(s)")
sys.exit(1 if _fails else 0)
