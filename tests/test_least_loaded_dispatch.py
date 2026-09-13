"""Guard — the lane fan uses DYNAMIC least-loaded dispatch, and it preserves the cross-vendor spread.

Static round-robin (arms[i % n]) head-of-line-blocks: a task bound to one slow lane queues its whole share while
fast lanes idle. _least_loaded_arm picks the arm with the MOST free capacity, scanning from the round-robin start
so ties still spread. Pins:
  · most-free arm wins (a busy/slow lane, low free, stops attracting work);
  · equal-load ties fall to the round-robin arm (start-ordered) → the cross-vendor SPREAD is seeded, not collapsed;
  · a cooling arm is skipped; every arm cooling → the round-robin pick (safe fallback);
  · dispatch.lane_free is a pure read: an idle lane reports its full cap.
Offline: free/cooling are injected — no network, no spend."""
import os, sys, tempfile

if not os.environ.get("SPENDGUARD_TEST_ISOLATED"):
    os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"
    os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="spendguard-lld-")
    _self = os.path.realpath(__file__)
    _root = os.path.realpath(os.path.dirname(__file__)) + os.sep
    if not _self.startswith(_root):
        raise SystemExit("refusing to re-exec a path outside the test directory: %s" % _self)
    os.execv(sys.executable, [sys.executable, _self])

from spendguard import lane_balance, dispatch

_fails = []
def ck(label, cond):
    if not cond:
        _fails.append(label)
    print(f"  [{'OK' if cond else 'FAIL'}] {label}")

ARMS = [("codex", "c"), ("zai-coding", "z"), ("gemini", "g")]
def pick(free, cooling, start=0):
    return lane_balance._least_loaded_arm(ARMS, start, lambda l: free.get(l, 0), lambda l: l in cooling)

print("-- most-free arm wins (a slow/busy lane, low free, stops attracting work) --")
ck("the lane with the most free capacity is picked", pick({"codex": 12, "zai-coding": 3, "gemini": 5}, set()) == ("codex", "c"))
ck("...regardless of the round-robin start (strictly-more free beats position)",
   pick({"codex": 12, "zai-coding": 3, "gemini": 5}, set(), start=1) == ("codex", "c"))
ck("a BUSY lane (0 free) is avoided", pick({"codex": 0, "zai-coding": 8, "gemini": 2}, set()) == ("zai-coding", "z"))

print("-- equal-load TIES fall to the round-robin arm → the cross-vendor SPREAD is seeded, not collapsed --")
EQ = {"codex": 8, "zai-coding": 8, "gemini": 8}
ck("start 0 → arm 0", pick(EQ, set(), start=0) == ("codex", "c"))
ck("start 1 → arm 1", pick(EQ, set(), start=1) == ("zai-coding", "z"))
ck("start 2 → arm 2", pick(EQ, set(), start=2) == ("gemini", "g"))
ck("start 3 wraps → arm 0 (round-robin spread across a batch)", pick(EQ, set(), start=3) == ("codex", "c"))

print("-- a cooling arm is skipped; every arm cooling → the round-robin pick (safe fallback) --")
ck("cooling codex → the next most-free non-cooling arm", pick({"codex": 12, "zai-coding": 8, "gemini": 4}, {"codex"}) == ("zai-coding", "z"))
ck("every arm cooling → arms[start] (unchanged round-robin fallback)",
   pick(EQ, {"codex", "zai-coding", "gemini"}, start=1) == ("zai-coding", "z"))

print("-- dispatch.lane_free: a pure read — an idle lane reports its full cap --")
_free_codex = dispatch.lane_free("codex")
ck("an idle lane's free == its configured cap (nonzero)", _free_codex >= 8)
ck("an unknown lane still returns a positive cap (never negative/zero)", dispatch.lane_free("no-such-lane-xyz") >= 1)

print(f"\n{'[FAIL]' if _fails else 'OK'} test_least_loaded_dispatch: {len(_fails)} failure(s)")
sys.exit(1 if _fails else 0)
