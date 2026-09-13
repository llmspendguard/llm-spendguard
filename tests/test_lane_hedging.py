"""TAIL-HEDGING in the lane fan — the per-CALL tail rescue that dynamic dispatch (per-LANE balancing) can't give.

When a task's PRIMARY lane stalls past `dispatch.lane_hedge_ms`, bulk_delegate fires a DUPLICATE on the most-free
OTHER lane and takes whichever returns text FIRST. Pins the contract that matters:
  · OFF by default (_hedge_ms=0): the plain single attempt, byte-for-byte the old path — no hedge, no extra call;
  · ON + a SLOW primary: the fast OTHER lane's answer wins, tagged hedged=True + hedge_peer=<the slow lane>;
  · ON + a FAST primary: it serves within the window → NO hedge is even submitted (one call, no tag);
  · the hedge is ALWAYS $0 — it runs no_metered_fallback=True even when the caller allowed the primary to bill.
Offline: adapters.call sleeps a controllable amount, dispatch/catalog stubbed — no LLM, no subprocess, no network.
The primary vs hedge LANE is made deterministic by stubbing dispatch.lane_free (most-free = the primary pick)."""
import os
import sys
import time
import tempfile

os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="sg-hedge-")
os.environ.setdefault("SPENDGUARD_TEST_ISOLATED", "1")
os.environ.setdefault("SPENDGUARD_NO_AUTOINSTALL", "1")
os.environ.pop("SPENDGUARD_DISPATCH_LANE_HEDGE_MS", None)     # start from OFF; each block sets it explicitly
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from spendguard import lane_balance, lane_catalog, lane_bandit, adapters, dispatch, lane_economics   # noqa: E402


def ck(name, cond):
    ok = bool(cond)
    print(("  [OK] " if ok else "  [FAIL] ") + name)
    return [] if ok else [name]


fails = []

# TWO lanes: one whose model sleeps (the tail), one that answers immediately. Names carry the timing so the
# stubbed adapters.call can branch on the model string alone (parsing a fixed convention, not a judgement).
ARMS = [("slowlane", "slow-m"), ("fastlane", "fast-m")]
SLOW_SLEEP_S = 0.6
lane_catalog.arms = lambda flt=None: list(ARMS)
lane_catalog.lane_provider = lambda l: {"slowlane": "provS", "fastlane": "provF"}.get(l)
lane_bandit._arm_cooling = lambda l, u: False
adapters._lane_cooling = lambda l: False
lane_economics.prompt_lane_reserved = lambda l: False
lane_bandit.arm_stats = lambda intent: {("slowlane", "slow-m"): {"winrate": 1.0, "trials": 2},
                                        ("fastlane", "fast-m"): {"winrate": 1.0, "trials": 2}}
dispatch.acquire = lambda *a, **k: 0.0
dispatch.release = lambda *a, **k: None


class _TimedRecorder:
    """adapters.call stand-in: records each call at START (before sleeping, so a still-running loser is still seen),
    then sleeps if the model is the slow one. Mirrors the real signature so an unknown kwarg fails here."""

    def __init__(self):
        self.calls = []

    def __call__(self, model, prompt, max_tokens=None, system=None, reasoning=None, schema=None,
                 timeout_s=None, sig=None, retries=2, files=None, _no_guard=False, no_metered_fallback=False,
                 images=None, no_substitution=False):
        assert max_tokens is not None or sig, "adapters.call needs max_tokens or sig (an output budget)"
        self.calls.append({"model": model, "prompt": prompt, "no_metered_fallback": no_metered_fallback})
        if "slow-m" in model:
            time.sleep(SLOW_SLEEP_S)
        return {"text": f"ans::{model}", "cost": 0}


_rec = _TimedRecorder()
adapters.call = _rec


def _free(mapping):
    return lambda l: mapping.get(l, 0)


print("-- OFF by default (_hedge_ms=0): a slow primary just runs slow — NO hedge, NO extra call, no tag --")
os.environ.pop("SPENDGUARD_DISPATCH_LANE_HEDGE_MS", None)
dispatch.lane_free = _free({"slowlane": 10, "fastlane": 5})   # most-free = slowlane → it is the primary pick
_rec.calls.clear()
r_off = lane_balance.bulk_delegate(["t"], "hedgeintent")
fails += ck("off: exactly ONE call was made (no duplicate)", len(_rec.calls) == 1)
fails += ck("off: served by the primary (slowlane), no 'hedged' tag", r_off[0].get("lane") == "slowlane" and not r_off[0].get("hedged"))

print("\n-- ON + SLOW primary: the fast OTHER lane wins the race, tagged hedged=True + hedge_peer=slowlane --")
os.environ["SPENDGUARD_DISPATCH_LANE_HEDGE_MS"] = "150"       # 150ms << the slow lane's 600ms → the primary times out
dispatch.lane_free = _free({"slowlane": 10, "fastlane": 5})   # primary = slowlane; hedge picks the other (fastlane)
_rec.calls.clear()
t0 = time.time()
r_on = lane_balance.bulk_delegate(["t"], "hedgeintent")       # default refuse_billed=False (primary MAY bill)
wall = time.time() - t0
row = r_on[0]
fails += ck("on: the FAST lane's answer won (served by fastlane, not the slow primary)", row.get("lane") == "fastlane")
fails += ck("on: row is tagged hedged=True", row.get("hedged") is True)
fails += ck("on: hedge_peer names the SLOW primary that was raced", row.get("hedge_peer") == "slowlane")
fails += ck("on: returned WITHOUT waiting for the slow primary (wall < the 600ms slow sleep)", wall < SLOW_SLEEP_S)
fails += ck("on: BOTH lanes were called (primary + hedge)", len(_rec.calls) == 2)
_hedge_call = [c for c in _rec.calls if "fast-m" in c["model"]]
_prim_call = [c for c in _rec.calls if "slow-m" in c["model"]]
fails += ck("on: the HEDGE call ran no_metered_fallback=True ($0 — can never bill)",
            bool(_hedge_call) and all(c["no_metered_fallback"] for c in _hedge_call))
fails += ck("on: the PRIMARY kept the caller's refuse_billed=False (only the hedge is force-$0)",
            bool(_prim_call) and all(c["no_metered_fallback"] is False for c in _prim_call))

print("\n-- ON + FAST primary: it serves within the window → NO hedge is submitted (one call, no tag) --")
os.environ["SPENDGUARD_DISPATCH_LANE_HEDGE_MS"] = "300"       # generous window; the fast primary returns well inside it
dispatch.lane_free = _free({"fastlane": 10, "slowlane": 5})   # most-free = fastlane → it is the primary
_rec.calls.clear()
r_fast = lane_balance.bulk_delegate(["t"], "hedgeintent")
fails += ck("fast-primary: exactly ONE call (the primary served in time — no hedge fired)", len(_rec.calls) == 1)
fails += ck("fast-primary: served by fastlane, no 'hedged' tag", r_fast[0].get("lane") == "fastlane" and not r_fast[0].get("hedged"))

os.environ.pop("SPENDGUARD_DISPATCH_LANE_HEDGE_MS", None)
print(f"\n{'[FAIL]' if fails else 'OK'} test_lane_hedging: {len(fails)} failure(s)")
sys.exit(1 if fails else 0)
