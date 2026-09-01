"""dispatch.try_admit — the NON-BLOCKING admission axis the blocking acquire() lacks. A saturated machine must make a
caller SHED (never spawn / never wait), so the hook fleet (honestreview/ccwatch/7thsense) can self-limit at the
source instead of piling up hundreds of waiting processes (the 2026-09-01 leak). Pins:

  1. at limit N, the first N calls admit and the (N+1)th SHEDS (returns None) — no blocking, no wait;
  2. releasing a handle frees its machine-wide (flock) slot for the next caller;
  3. with cross-process gating OFF (no fcntl / XP_OFF), every call admits via a no-op handle whose release is safe —
     a non-POSIX host is never blocked.

Cross-process by flock, so this holds ACROSS processes; here it's exercised in one process (separate open
descriptions still contend), which is enough to prove the slot accounting. Zero spend, no network."""
import os
import sys

from spendguard import dispatch

fails = []
def ck(name, cond):
    print(("  [OK] " if cond else "  [FAIL] ") + name)
    if not cond:
        fails.append(name)

POOL = "test-try-admit-guard"

# 1. at limit=2, two admit and the third sheds
a = dispatch.try_admit(POOL, limit=2)
b = dispatch.try_admit(POOL, limit=2)
c = dispatch.try_admit(POOL, limit=2)
ck("limit=2 → first two admit", bool(a) and bool(b))
ck("limit=2 → third SHEDS (None), no block/wait", c is None)

# 2. releasing a handle frees a slot for the next caller
a.release()
d = dispatch.try_admit(POOL, limit=2)
ck("release frees a slot → next caller admits", bool(d))
b.release()
d.release()

# 3. fully released → admits again
e = dispatch.try_admit(POOL, limit=2)
ck("fully released → admits again", bool(e))
if e:
    e.release()

# 4. gating OFF → always admit via a no-op handle, release never raises
os.environ["SPENDGUARD_DISPATCH_XP_OFF"] = "1"
try:
    handles = [dispatch.try_admit(POOL, limit=1) for _ in range(5)]     # limit=1 but gating off → all admit
    ck("XP_OFF → every call admits (never sheds)", all(handles))
    for h in handles:
        h.release()                                                    # no-op; must not raise
    ck("no-op handle .release() is safe", True)
finally:
    os.environ.pop("SPENDGUARD_DISPATCH_XP_OFF", None)

print(("\n[OK] " if not fails else "\n[FAIL] ") + "dispatch_try_admit: %d failure(s)" % len(fails))
sys.exit(1 if fails else 0)
