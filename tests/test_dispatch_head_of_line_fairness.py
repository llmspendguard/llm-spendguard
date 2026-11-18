"""Head-of-line fairness: a SATURATED vendor must not starve a HEALTHY vendor of the machine-wide global slot.

The governor takes the global slot LAST — after a call clears its own vendor's gates — so a call parked waiting on
its saturated vendor holds only that vendor's resources, never a global slot. Before that reorder the global slot was
taken FIRST, so N calls queued behind one busy vendor held N of the global slots and starved calls to every other
vendor (the head-of-line bug this proves is fixed).

Construction (hermetic, no live calls): global ceiling = 2. Vendor A's connection cap is pinned to 1 and one A-call
holds it; two more A-calls then PARK at the connection window. Under the fix those parked A-calls hold ZERO global
slots, so vendor B (healthy) still gets one of the two global slots and admits immediately. Under the old global-first
order the parked A-calls would hold both global slots and B would time out."""
import os
import sys
import tempfile
import threading
import time
import functools
import pathlib

os.environ["SPENDGUARD_DISPATCH_XP_OFF"] = "1"                 # in-process only (no flock) — isolate the global/bucket/conn gates
os.environ["SPENDGUARD_DISPATCH_GLOBAL_CONCURRENCY"] = "2"     # tiny machine-wide ceiling so the contention is on the global slot
os.environ["SPENDGUARD_ROUTE_THROUGH_QUEUE"] = "0"
os.environ.setdefault("SPENDGUARD_NO_AUTOINSTALL", "1")
if not os.environ.get("SPENDGUARD_TEST_ISOLATED"):
    os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"
    os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="sg-hol-")
    os.execv(sys.executable, [sys.executable] + sys.argv)

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))
from spendguard import dispatch   # noqa: E402

print = functools.partial(print, flush=True)
fails = 0


def ck(label, cond, extra=""):
    global fails
    if not cond:
        fails += 1
    print(f"  [{'OK' if cond else 'FAIL'}] {label}" + (f"  ({extra})" if extra else ""))


SATURATED, HEALTHY = "satvendor", "healthyvendor"
dispatch.reset_connection_window(SATURATED)
dispatch.reset_connection_window(HEALTHY)
dispatch._LEARNED.learn(SATURATED, conn=1, source="test-pin")   # the saturated vendor admits exactly ONE at a time

release_holder = threading.Event()
holder_admitted = threading.Event()


def _hold_saturated_slot():
    """Admit ONE call to the saturated vendor and keep its global+conn+bucket slots held until told to release."""
    dispatch.acquire(SATURATED, f"{SATURATED}:m", 10.0)
    holder_admitted.set()
    release_holder.wait(10.0)
    dispatch.release(SATURATED, f"{SATURATED}:m")


def _park_on_saturated():
    """A call to the saturated vendor that PARKS at the connection window (cap 1, already held). Short deadline so it
    self-clears via DispatchTimeout after the assertion — under the fix it is holding NO global slot while parked."""
    try:
        dispatch.acquire(SATURATED, f"{SATURATED}:m", 1.5)
        dispatch.release(SATURATED, f"{SATURATED}:m")           # (only reached if it somehow admits — then free it)
    except dispatch.DispatchTimeout:
        pass


holder = threading.Thread(target=_hold_saturated_slot, daemon=True)
holder.start()
ck("the saturated vendor admits its one call", holder_admitted.wait(5.0))

parkers = [threading.Thread(target=_park_on_saturated, daemon=True) for _ in range(2)]
for p in parkers:
    p.start()
time.sleep(0.4)                                                # let both parkers reach and park at the connection window

# THE PROOF: a healthy vendor must still get a global slot (1 of 2 is held by the holder; the two parked
# saturated-vendor calls hold NONE under the fix, both under the old global-first order).
t0 = time.time()
healthy_ok, waited, err = False, None, None
try:
    waited = dispatch.acquire(HEALTHY, f"{HEALTHY}:m", 1.0)    # deadline 1.0s < the parkers' 1.5s, so they are still parked
    healthy_ok = True
    dispatch.release(HEALTHY, f"{HEALTHY}:m")
except dispatch.DispatchTimeout as e:
    err = str(e)
elapsed = time.time() - t0

ck("a healthy vendor admits despite a saturated vendor's parked backlog (no head-of-line on the global slot)",
   healthy_ok, f"elapsed={elapsed:.2f}s err={err}")
ck("the healthy admit was prompt (a free global slot, not a near-deadline scrape)",
   healthy_ok and elapsed < 0.8, f"elapsed={elapsed:.2f}s")

# A direct read of the global semaphore: with only the holder in flight, at least one of the two slots is free —
# the parked saturated-vendor calls are NOT occupying global slots.
free_global = getattr(dispatch._GOV._global, "_value", None)
ck("the global semaphore has a free slot while saturated-vendor calls are parked",
   free_global is not None and free_global >= 1, f"free_global={free_global}")

release_holder.set()
holder.join(5.0)
for p in parkers:
    p.join(5.0)

print(f"\n{'OK' if fails == 0 else '[FAIL]'} test_dispatch_head_of_line_fairness: {fails} failure(s)")
sys.exit(1 if fails else 0)
