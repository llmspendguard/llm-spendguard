"""GUARD — a confined bulk fan whose lanes yield NO viable arm WIDENS to the default lanes instead of dead-ending.

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

from spendguard import lane_balance, adapters, calls, lane_catalog, lane_economics, dispatch   # noqa: E402

fails = []


def ck(name, cond):
    print(("  [OK] " if cond else "  [FAIL] ") + name)
    if not cond:
        fails.append(name)


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

# ── (a) confined empty, unconfined non-empty → WIDEN and run ──
print("-- (a) confined lanes with no viable arm → widen to the default lanes and RUN --")
lane_balance._bulk_arms = lambda intent, lanes=None: ([] if lanes else [DEFAULT_ARM])
res = lane_balance.bulk_delegate(list(TASKS), "fresh:intent", lanes=["confined-lane"], force=True)
ck("confined-empty + default-available → the fan RUNS (no 'no_viable_lane' rows)",
   len(res) == len(TASKS) and all((r or {}).get("reason") != "no_viable_lane" for r in res))
ck("every task was served on the widened default lane (text present)",
   all((r or {}).get("text") == "ans" for r in res))

# ── (b) NOTHING viable anywhere → honest refusal (we widen, never invent) ──
print("\n-- (b) no lane viable at all → honest no_viable_lane, never invents a lane --")
lane_balance._bulk_arms = lambda intent, lanes=None: []
res_none = lane_balance.bulk_delegate(list(TASKS), "fresh:intent", lanes=["confined-lane"], force=True)
ck("no lane viable at all → 'no_viable_lane' rows (widen never invents a lane)",
   all((r or {}).get("reason") == "no_viable_lane" and (r or {}).get("text") is None for r in res_none))

# ── (c) an UNCONFINED caller is UNCHANGED — the widen branch is confined-only ──
print("\n-- (c) an unconfined (lanes=None) caller is unchanged: widen branch not triggered --")
_calls_with_no_lanes = {"n": 0}


def _arms_track(intent, lanes=None):
    if not lanes:
        _calls_with_no_lanes["n"] += 1
    return [DEFAULT_ARM] if not lanes else []


lane_balance._bulk_arms = _arms_track
res_unconf = lane_balance.bulk_delegate(list(TASKS), "fresh:intent", force=True)   # lanes=None
ck("unconfined caller runs directly (served), and _bulk_arms(lanes=None) is called exactly once (no widen retry)",
   all((r or {}).get("text") == "ans" for r in res_unconf) and _calls_with_no_lanes["n"] == 1)

print(f"\n{'[FAIL]' if fails else 'OK'} test_bulk_delegate_confined_widen: {len(fails)} failure(s)")
sys.exit(1 if fails else 0)
