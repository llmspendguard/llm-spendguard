"""GUARD — lane confinement refuses by default and widens only by explicit opt-in.

Measured 2026-09-21: a bulk job confined to lanes= on a FRESH intent (no bandit rating yet, or confined lanes with
no advisor.lane_models) returned 766 EMPTY rows and did no work — bulk_delegate's no_viable_lane path refused the
whole fan, no checkpoint written, nothing usable. The fix: when the CONFINED _bulk_arms is empty but the UNCONFINED
(default delegate lanes) _bulk_arms is not, run UNCONFINED (loud) so the tasks RUN — the same widening a caller does
by hand with lanes=None, automated. This pins:
  (a) confined lanes with no viable arm + a non-empty default set → the fan RUNS on the default lane(s), every task
      served (NOT a wall of 'no_viable_lane' rows);
  (b) NO lane viable at all (confined AND unconfined empty) → still the honest 'no_viable_lane' refusal (we widen,
      we never invent a lane);
  (c) an UNCONFINED caller (lanes=None) is UNCHANGED — the widen branch is confined-only.
Hermetic: _bulk_arms / adapters.call / dispatch / lane_catalog / lane_economics stubbed; isolated home; no spend.
"""
import os
import sys
import tempfile

os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="sg-widen-")
os.environ.setdefault("SPENDGUARD_TEST_ISOLATED", "1")
os.environ.setdefault("SPENDGUARD_NO_AUTOINSTALL", "1")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from _script_checks import record_script_check  # noqa: E402

from spendguard import lane_balance, adapters, calls, lane_catalog, lane_economics, dispatch   # noqa: E402

fails = []


DEFAULT_ARM = ("gemini", "g-model")
TASKS = ["t1", "t2"]

# the fan machinery is a no-op harness; only _bulk_arms' confined-vs-unconfined answer drives the branch under test
lane_catalog.lane_provider = lambda l: "google"
lane_economics.prompt_lane_reserved = lambda l: False
adapters._lane_cooling = lambda l: False
adapters.provider_for = lambda m: (m or "").split(":", 1)[0]
calls.set_context = lambda **k: None
dispatch.acquire_or_none = lambda *a, **k: 0.0
dispatch.release = lambda *a, **k: None
adapters.call = lambda model, task, **kw: {"text": "ans", "provider": "google", "model": "g-model",
                                           "executor": "gemini", "cost": 0.0, "error": None, "reason": None}

# ── (a) confined empty, unconfined non-empty → REFUSE and name the declaration ──
print("-- (a) confined lanes with no viable arm → refuse without widening --")
lane_balance._bulk_arms = lambda intent, lanes=None: ([] if lanes else [DEFAULT_ARM])
res = lane_balance.bulk_delegate(list(TASKS), "fresh:intent", lanes=["confined-lane"], force=True)
record_script_check(fails, "confined-empty refuses and names advisor.lane_models",
   all((r or {}).get("reason") == "no_viable_lane" and "advisor.lane_models" in (r or {}).get("error", "")
       for r in res))

# ── (b) explicit opt-in widens ──
print("\n-- (b) --lanes-or-default explicitly widens and runs --")
res_opt_in = lane_balance.bulk_delegate(list(TASKS), "fresh:intent", lanes=["confined-lane"],
                                        lanes_or_default=True, force=True)
record_script_check(fails, "explicit widening opt-in serves on the default arm",
   all((r or {}).get("text") == "ans" for r in res_opt_in))

# ── (c) NOTHING viable anywhere → honest refusal ──
print("\n-- (c) no lane viable at all → honest no_viable_lane --")
lane_balance._bulk_arms = lambda intent, lanes=None: []
res_none = lane_balance.bulk_delegate(list(TASKS), "fresh:intent", lanes=["confined-lane"], force=True)
record_script_check(fails, "no lane viable at all → 'no_viable_lane' rows (widen never invents a lane)",
   all((r or {}).get("reason") == "no_viable_lane" and (r or {}).get("text") is None for r in res_none))

# ── (d) an UNCONFINED caller is UNCHANGED ──
print("\n-- (d) an unconfined (lanes=None) caller is unchanged --")
_calls_with_no_lanes = {"n": 0}


def _arms_track(intent, lanes=None):
    if not lanes:
        _calls_with_no_lanes["n"] += 1
    return [DEFAULT_ARM] if not lanes else []


lane_balance._bulk_arms = _arms_track
res_unconf = lane_balance.bulk_delegate(list(TASKS), "fresh:intent", force=True)   # lanes=None
record_script_check(fails, "unconfined caller runs directly (served), and _bulk_arms(lanes=None) is called exactly once (no widen retry)",
   all((r or {}).get("text") == "ans" for r in res_unconf) and _calls_with_no_lanes["n"] == 1)

print(f"\n{'[FAIL]' if fails else 'OK'} test_bulk_delegate_confined_widen: {len(fails)} failure(s)")
sys.exit(1 if fails else 0)
