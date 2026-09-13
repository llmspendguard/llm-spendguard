"""Cross-process METERED admission: the flock cap now covers metered vendors too (was lane-only), so N concurrent
runs SHARE ONE per-vendor cap and don't 429-storm the account. Proves Governor.acquire routes a metered (vendor:)
call through the cross-process slot (before this change metered skipped that layer): hold the single vendor slot and
a fresh metered acquire for that vendor TIMES OUT. Also pins the per-vendor limit override. Isolated home; skips
cleanly where fcntl is unavailable (the guarantee is too)."""
import os
import sys
import tempfile

if not os.environ.get("SPENDGUARD_TEST_ISOLATED"):
    os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"
    os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="spendguard-xpm-")
    os.environ["SPENDGUARD_DISPATCH_VENDOR_CONCURRENCY_TESTVENDOR"] = "1"   # 1 cross-process slot for the test vendor
    os.execv(sys.executable, [sys.executable] + sys.argv)

from spendguard import dispatch, adapters   # noqa: E402

fails = 0


def ck(label, cond):
    global fails
    if not cond:
        fails += 1
    print(f"  [{'OK' if cond else 'FAIL'}] {label}")


if dispatch._xp_off():
    print("  [SKIP] fcntl unavailable / XP off — cross-process gating not supported here (in-process bound still holds)")
    print("\nOK test_dispatch_metered_cross_process: 0 failure(s)")
    sys.exit(0)

adapters._lane_for = lambda v: None                  # every vendor is METERED (no active lane) → key = vendor:<v>

key, limit, _rpm, is_lane = dispatch._GOV._key_and_limit("testvendor", "testvendor:m")
ck("a metered vendor keys as vendor: (not a lane)", key == "vendor:testvendor" and not is_lane)
ck("the PER-VENDOR override is applied (vendor_concurrency_testvendor=1)", limit == 1)

# Hold the vendor's SINGLE cross-process slot directly (as another process would). A metered acquire must now contend
# for it — before Build #3 metered never touched the cross-process layer, so this acquire would NOT have blocked.
held = dispatch._acquire_xp("vendor:testvendor", 1, 10)
timed_out = False
try:
    dispatch.acquire("testvendor", "testvendor:m", 0.6)
except dispatch.DispatchTimeout:
    timed_out = True
finally:
    held.release()
ck("a METERED acquire is CROSS-GATED — it times out when the vendor's cross-process slot is held", timed_out)

# once released, the metered vendor slot is free again (the OS drops the flock — no stale lock), acquire/release balanced
dispatch.acquire("testvendor", "testvendor:m", 3)
dispatch.release("testvendor", "testvendor:m")
ck("after release the metered vendor slot is free again (acquire+release balanced, no stale lock)", True)

# a DIFFERENT metered vendor is independent (its own key/slots) — one vendor saturated never blocks another
held2 = dispatch._acquire_xp("vendor:testvendor", 1, 10)
other_ok = False
try:
    dispatch.acquire("othervendor", "othervendor:m", 1)   # different vendor key → not blocked by testvendor's held slot
    dispatch.release("othervendor", "othervendor:m")
    other_ok = True
finally:
    held2.release()
ck("a metered acquire on a DIFFERENT vendor is not blocked (per-vendor keys, independent caps)", other_ok)

print(f"\n{'[FAIL]' if fails else 'OK'} test_dispatch_metered_cross_process: {fails} failure(s)")
sys.exit(1 if fails else 0)
