"""GUARD — advisor.default_reasoning=best-value is floor-IMPROVING, never floor-LOWERING (regression from 9e19892).

Observed live 2026-09-21: honestreview's pinned-model hooks (model=config.advisor_model(), intent=…, NO reasoning)
INHERITED the best-value default, which ran the cross-model advisor path (pin_model=False) -> a billed
recommend_models call returning a cold-intent "no pick", and a SpendGateRefused from that advisor call was SWALLOWED
to _pick=None -> the call continued to an empty/unparseable result a fail-closed consumer read as NOT REVIEWED. Pins:
  (a) req#2 — a call that PINNED model= and merely INHERITED the default best-value titrates EFFORT ONLY
      (pin_model=True): it never re-picks the model, and never fires the cross-model recommend_models spend;
  (b) req#1 — with zero data that pinned call keeps its OWN model and returns a NORMAL result (not empty);
  (c) req#3 — a deliberate stop (SpendGateRefused) from the advisor PROPAGATES as a TYPED stop, never swallowed to an
      empty result the gate meant to stop;
  (d) an EXPLICIT reasoning="best-value" (the caller DELEGATED the model) still resolves cross-model (pin_model=False).
Hermetic: best_value.select_model_effort + adapters._call_guarded stubbed; SPENDGUARD_DEFAULT_REASONING=best-value; no spend.
"""
import os
import sys
import tempfile

os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="sg-bvfloor-")
os.environ["SPENDGUARD_DEFAULT_REASONING"] = "best-value"   # arm the global default (the regression's trigger)
os.environ.setdefault("SPENDGUARD_TEST_ISOLATED", "1")
os.environ.setdefault("SPENDGUARD_NO_AUTOINSTALL", "1")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from spendguard import adapters, best_value   # noqa: E402
from spendguard.gate import SpendGateRefused   # noqa: E402

fails = []


def ck(name, cond):
    print(("  [OK] " if cond else "  [FAIL] ") + name)
    if not cond:
        fails.append(name)


PINNED = "acme:pinned"
seen = {}


def _served(model, prompt, **kw):
    return {"text": "served", "provider": "acme", "model": model.split(":", 1)[1], "cost": 0.001,
            "in_tok": 5, "out_tok": 3, "error": None, "executor": "api"}


_saved = (adapters._call_guarded, best_value.select_model_effort)
adapters._call_guarded = _served

try:
    # ── (a)+(b): a pinned call INHERITING the default best-value -> pin_model=True, keeps the model, non-empty ──
    print("-- (a)/(b) default-inherited best-value on a pinned call: effort-only, keeps the model, normal result --")

    def _sel_nopick(intent, requested_model, pin_model=False, quality_target=None, prompt=None):
        seen["pin_model"] = pin_model
        return {"model": None, "effort": None, "why": "best-value: no data", "considered": {}}

    best_value.select_model_effort = _sel_nopick
    seen.clear()
    r = adapters.call(PINNED, "p", intent="cold-intent")            # inherits the default best-value, no reasoning passed
    ck("(req#2) a pinned call inheriting the default best-value titrates EFFORT ONLY (pin_model=True)",
       seen.get("pin_model") is True)
    ck("(req#1) no data -> keeps the PINNED model and returns a NORMAL result (floor-preserving, not empty)",
       r.get("text") == "served" and r.get("model") == "pinned")

    # ── (d): an EXPLICIT best-value (delegated), unpinned -> pin_model=False (cross-model) ──
    print("\n-- (d) an EXPLICIT reasoning='best-value' still delegates the model (pin_model=False) --")
    seen.clear()
    adapters.call(PINNED, "p", intent="cold-intent", reasoning="best-value")
    ck("(delegated) an EXPLICIT reasoning='best-value' resolves cross-model (pin_model=False)",
       seen.get("pin_model") is False)

    # ── (c): a deliberate stop from the advisor PROPAGATES as a typed stop, never swallowed to empty ──
    print("\n-- (c) a SpendGateRefused from the advisor propagates as a TYPED stop (not swallowed to empty) --")

    def _sel_refuse(intent, requested_model, pin_model=False, **kw):
        raise SpendGateRefused("advisor meta-budget exhausted")

    best_value.select_model_effort = _sel_refuse
    propagated = False
    try:
        adapters.call(PINNED, "p", intent="cold-intent", reasoning="best-value")
    except SpendGateRefused:
        propagated = True                                          # the ONLY catch: the exact type req#3 asserts propagates
    ck("(req#3) a SpendGateRefused from the advisor PROPAGATES (never swallowed to _pick=None -> an empty result)",
       propagated)
finally:
    adapters._call_guarded, best_value.select_model_effort = _saved

print(f"\n{'[FAIL]' if fails else 'OK'} test_best_value_floor_no_data: {len(fails)} failure(s)")
sys.exit(1 if fails else 0)
