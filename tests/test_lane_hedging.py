"""TAIL-HEDGING in the lane fan — the per-CALL tail rescue that dynamic dispatch (per-LANE balancing) can't give.

When a task's PRIMARY lane stalls past the hedge window, bulk_delegate fires a DUPLICATE on the most-free OTHER
lane and takes whichever returns text FIRST. Pins the contract that matters:
  · OFF by default (no hedge_ms param, no config): the plain single attempt — no hedge, no extra call;
  · the `hedge_ms` PARAM turns it on per-call (a small-fan caller opts in WITHOUT a global that arms bulk too);
  · ON + a SLOW primary + a FREE other lane: the fast lane's answer wins, tagged hedged=True + hedge_peer=<slow>;
  · SPARE-CAPACITY GATE — ON + a SLOW primary but the other lane SATURATED (lane_free==0): NO hedge fires (this is
    what auto-avoids the measured N=96 saturation trap — a duplicate never piles onto busy slots);
  · ON + a FAST primary: it serves within the window → NO hedge is even submitted;
  · the hedge is ALWAYS $0 — it runs no_metered_fallback=True even when the caller allowed the primary to bill;
  · the config/env path still works when the param is omitted (hedge_ms=None → dispatch.lane_hedge_ms).
Offline: adapters.call sleeps a controllable amount, dispatch/catalog stubbed — no LLM, no subprocess, no network.
The primary vs hedge LANE and the spare capacity are made deterministic by stubbing dispatch.lane_free."""
import os
import sys
import time
import tempfile

os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="sg-hedge-")
os.environ.setdefault("SPENDGUARD_TEST_ISOLATED", "1")
os.environ.setdefault("SPENDGUARD_NO_AUTOINSTALL", "1")
os.environ.pop("SPENDGUARD_DISPATCH_LANE_HEDGE_MS", None)     # start from OFF; blocks set it explicitly
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


print("-- OFF by default (no hedge_ms param, no config): a slow primary just runs slow — NO hedge, no extra call --")
os.environ.pop("SPENDGUARD_DISPATCH_LANE_HEDGE_MS", None)
dispatch.lane_free = _free({"slowlane": 10, "fastlane": 5})   # most-free = slowlane → it is the primary pick
_rec.calls.clear()
r_off = lane_balance.bulk_delegate(["t"], "hedgeintent")
fails += ck("off: exactly ONE call was made (no duplicate)", len(_rec.calls) == 1)
fails += ck("off: served by the primary (slowlane), no 'hedged' tag", r_off[0].get("lane") == "slowlane" and not r_off[0].get("hedged"))

print("\n-- hedge_ms PARAM + SLOW primary + a FREE other lane: the fast lane wins, tagged hedged=True + hedge_peer --")
dispatch.lane_free = _free({"slowlane": 10, "fastlane": 5})   # primary = slowlane; hedge picks the other (fastlane, free=5)
_rec.calls.clear()
t0 = time.time()
r_on = lane_balance.bulk_delegate(["t"], "hedgeintent", hedge_ms=150)   # 150ms << the slow 600ms → primary times out
wall = time.time() - t0
row = r_on[0]
fails += ck("param on: the FAST lane's answer won (served by fastlane, not the slow primary)", row.get("lane") == "fastlane")
fails += ck("param on: row tagged hedged=True + hedge_peer=slowlane", row.get("hedged") is True and row.get("hedge_peer") == "slowlane")
fails += ck("param on: returned WITHOUT waiting for the slow primary (wall < 600ms slow sleep)", wall < SLOW_SLEEP_S)
fails += ck("param on: BOTH lanes were called (primary + hedge)", len(_rec.calls) == 2)
_hedge_call = [c for c in _rec.calls if "fast-m" in c["model"]]
_prim_call = [c for c in _rec.calls if "slow-m" in c["model"]]
fails += ck("param on: the HEDGE call ran no_metered_fallback=True ($0 — can never bill)",
            bool(_hedge_call) and all(c["no_metered_fallback"] for c in _hedge_call))
fails += ck("param on: the PRIMARY kept the caller's refuse_billed=False (only the hedge is force-$0)",
            bool(_prim_call) and all(c["no_metered_fallback"] is False for c in _prim_call))

print("\n-- SPARE-CAPACITY GATE: SLOW primary but the other lane SATURATED (free==0) → NO hedge (avoids the pile-on) --")
dispatch.lane_free = _free({"slowlane": 10, "fastlane": 0})   # fastlane has NO free slot → the saturated-bulk case
_rec.calls.clear()
r_sat = lane_balance.bulk_delegate(["t"], "hedgeintent", hedge_ms=150)
fails += ck("saturated: NO hedge fired — exactly ONE call (the duplicate never piles onto a busy lane)", len(_rec.calls) == 1)
fails += ck("saturated: served by the slow primary, no 'hedged' tag (it waited rather than swarm)",
            r_sat[0].get("lane") == "slowlane" and not r_sat[0].get("hedged"))

print("\n-- FAST primary: it serves within the window → NO hedge is submitted (one call, no tag) --")
dispatch.lane_free = _free({"fastlane": 10, "slowlane": 5})   # most-free = fastlane → it is the primary
_rec.calls.clear()
r_fast = lane_balance.bulk_delegate(["t"], "hedgeintent", hedge_ms=300)
fails += ck("fast-primary: exactly ONE call (the primary served in time — no hedge fired)", len(_rec.calls) == 1)
fails += ck("fast-primary: served by fastlane, no 'hedged' tag", r_fast[0].get("lane") == "fastlane" and not r_fast[0].get("hedged"))

print("\n-- config/env fallback: with NO param, hedge_ms comes from dispatch.lane_hedge_ms (env) — still works --")
os.environ["SPENDGUARD_DISPATCH_LANE_HEDGE_MS"] = "150"       # the config surface, used only when the param is omitted
dispatch.lane_free = _free({"slowlane": 10, "fastlane": 5})
_rec.calls.clear()
r_env = lane_balance.bulk_delegate(["t"], "hedgeintent")      # no hedge_ms param → falls back to the env/config
fails += ck("env fallback: hedging fired from config (fast lane won, tagged)", r_env[0].get("lane") == "fastlane" and r_env[0].get("hedged") is True)
os.environ.pop("SPENDGUARD_DISPATCH_LANE_HEDGE_MS", None)

print(f"\n{'[FAIL]' if fails else 'OK'} test_lane_hedging: {len(fails)} failure(s)")
sys.exit(1 if fails else 0)
