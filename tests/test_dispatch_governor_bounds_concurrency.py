"""The DISPATCH GOVERNOR bounds in-flight calls per vendor/lane, so cross-LLM work at scale QUEUES instead of
thrashing — and a call that can never get a slot is an honest DEADLINE, never a hang.

Why this guard exists. fan_out spawns one pool per call sized to the vendor count — correct for one panel, wrong
for many at once (honestreview reviews 50 files x 4 vendors → ~200 calls in flight, and the subprocess lanes
cold-start-thrash while metered vendors 429-storm). dispatch.py caps concurrency per (vendor|lane) and makes the
overflow wait. This pins three properties that must never regress:

  1. in-flight NEVER exceeds the configured limit, even under a burst of threads (the whole point);
  2. a queued call that cannot get a slot within its deadline raises DispatchTimeout (→ DEADLINE_EXCEEDED),
     rather than blocking forever — the honesty invariant under load;
  3. the kill switch (SPENDGUARD_DISPATCH_OFF=1) makes acquire a no-op, so spend discipline never depends on the
     scheduler being correct.

Offline, isolated home, zero network — the governor is pure threading/timing.
"""
import os
import sys
import tempfile
import threading
import time

if not os.environ.get("SPENDGUARD_TEST_ISOLATED"):
    os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"
    os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="spendguard-dispatch-")
    os.execv(sys.executable, [sys.executable] + sys.argv)

from spendguard import dispatch   # noqa: E402

fails = 0


def ck(label, cond, extra=""):
    global fails
    if not cond:
        fails += 1
    print(f"  [{'OK' if cond else 'FAIL'}] {label}{('  — ' + extra) if extra and not cond else ''}")


# ── 1. concurrency is bounded AND actually reaches the cap ───────────────────────────────────────────────────
LIMIT = 2
os.environ["SPENDGUARD_DISPATCH_VENDOR_CONCURRENCY"] = str(LIMIT)
cur, peak, _lk = 0, 0, threading.Lock()


def _worker():
    global cur, peak
    dispatch.acquire("acmevendor", "acme-model", deadline_s=10)
    try:
        with _lk:
            cur += 1
            peak = max(peak, cur)
        time.sleep(0.08)              # hold the slot long enough for a real burst to contend
    finally:
        with _lk:
            cur -= 1
        dispatch.release("acmevendor", "acme-model")


threads = [threading.Thread(target=_worker) for _ in range(8)]
for t in threads:
    t.start()
for t in threads:
    t.join()
ck(f"peak concurrency ({peak}) never exceeded the limit ({LIMIT})", peak <= LIMIT, f"peak={peak}")
ck("...and reached the limit (real parallelism up TO the cap, not serialized)", peak == LIMIT, f"peak={peak}")
ck("every slot was released (nothing left in flight)", cur == 0, f"cur={cur}")


# ── 2. a call that cannot get a slot within its deadline is an HONEST DispatchTimeout, not a hang ─────────────
os.environ["SPENDGUARD_DISPATCH_VENDOR_CONCURRENCY"] = "1"
dispatch.acquire("busyvendor", "m", deadline_s=10)     # occupy the single slot
timed_out = False
t0 = time.monotonic()
try:
    dispatch.acquire("busyvendor", "m", deadline_s=0.4)   # a second caller cannot get in
except dispatch.DispatchTimeout:
    timed_out = True
waited = time.monotonic() - t0
dispatch.release("busyvendor", "m")
ck("a queued call with no slot raises DispatchTimeout (honest deadline, not a silent hang)", timed_out)
ck("...and it gave up at ~its deadline, not forever", waited < 3.0, f"waited={waited:.2f}s")


# ── 3. the kill switch disables the governor entirely ────────────────────────────────────────────────────────
os.environ["SPENDGUARD_DISPATCH_OFF"] = "1"
os.environ["SPENDGUARD_DISPATCH_VENDOR_CONCURRENCY"] = "1"
# With OFF, two acquires on a limit-1 key both succeed instantly (no bounding).
w1 = dispatch.acquire("offvendor", "m", deadline_s=1)
w2 = dispatch.acquire("offvendor", "m", deadline_s=1)
ck("kill switch SPENDGUARD_DISPATCH_OFF=1 makes acquire a no-op (returns 0, never blocks)",
   w1 == 0.0 and w2 == 0.0, f"w1={w1} w2={w2}")
dispatch.release("offvendor", "m")
dispatch.release("offvendor", "m")
del os.environ["SPENDGUARD_DISPATCH_OFF"]

print(f"\n{'[FAIL]' if fails else 'OK'} test_dispatch_governor_bounds_concurrency: {fails} failure(s)")
sys.exit(1 if fails else 0)
