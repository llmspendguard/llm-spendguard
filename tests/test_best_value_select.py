"""Guard — reasoning="best-value" resolves (model, effort) AGENTICALLY from the measured learnings.

best_value delegates: EFFORT from the effort-titration fact (models.effort_for), MODEL from the advisor's agentic
ranker (advisor.recommend_models) — no hand-picked threshold in best_value itself. Pins:
  · pin_model=True titrates the EFFORT for the named model only (never swaps it — panel diversity)
  · cross-model picks the advisor's model and rides that model's learned effort
  · no measured basis → model=None (caller keeps its named model — honest)
  · a deliberate stop from the advisor (spend refusal) PROPAGATES, never swallowed to a silent no-pick
  · in adapters.call the sentinel is CONSUMED, model swapped to the pick, effort applied, call PINNED
"""
import os, sys, tempfile
if not os.environ.get("SPENDGUARD_TEST_ISOLATED"):
    os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"
    os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="spendguard-test-")
    _self = os.path.realpath(__file__)
    _root = os.path.realpath(os.path.dirname(__file__)) + os.sep
    if not _self.startswith(_root):
        raise SystemExit("refusing to re-exec a path outside the test directory: %s" % _self)
    os.execv(sys.executable, [sys.executable, _self])

from spendguard import models, advisor, best_value, adapters, calls, gate

failures = 0
def check(label, cond):
    global failures
    ok = bool(cond)
    if not ok:
        failures += 1
    print(f"  [{'OK' if ok else 'FAIL'}] {label}")


INTENT = "t:review"

print("-- pin_model: titrate the EFFORT of the named model only (never swap it) --")
models.record_effort("openai:gpt-5.5", INTENT, "low")
p = best_value.select_model_effort(INTENT, "openai:gpt-5.5", pin_model=True)
check("pinned pick keeps the named model", p["model"] == "openai:gpt-5.5")
check("pinned pick applies the learned effort", p["effort"] == "low")
check("a pinned model with no learned effort resolves to None (family floor stands)",
      best_value.select_model_effort(INTENT, "zai:glm-5.2", pin_model=True)["model"] is None)

print("-- cross-model: the advisor picks the MODEL (agentic), effort rides its learned fact --")
models.record_effort("openai:gpt-5-mini", INTENT, "minimal")
_orig_rec = advisor.recommend_models
advisor.recommend_models = lambda *a, **k: {"top": [{"id": "openai:gpt-5-mini", "why": "cheapest that holds"}],
                                            "ranked_from": 3, "note": None}
try:
    q = best_value.select_model_effort(INTENT, "openai:gpt-5.5")
    check("model came from the advisor's agentic pick", q["model"] == "openai:gpt-5-mini")
    check("effort came from that model's titration fact", q["effort"] == "minimal")
    check("the choice records its source (not a black box)", q["considered"]["source"] == "advisor.recommend_models")
finally:
    advisor.recommend_models = _orig_rec

print("-- no evidence → model=None (keep the named model) --")
advisor.recommend_models = lambda *a, **k: {"top": [], "note": "no evidence yet for this intent"}
try:
    check("cold intent resolves to None model", best_value.select_model_effort("t:cold", "openai:gpt-5.5")["model"] is None)
finally:
    advisor.recommend_models = _orig_rec

print("-- a deliberate stop from the advisor PROPAGATES (never swallowed) --")
_Stop = gate.deliberate_stop_types()[0]
def _raise_stop(*a, **k):
    raise _Stop("planted spend refusal")
advisor.recommend_models = _raise_stop
try:
    best_value.select_model_effort(INTENT, "openai:gpt-5.5")
    check("deliberate stop was (wrongly) swallowed", False)
except gate.deliberate_stop_types():
    check("deliberate stop propagated out of best_value.select", True)
except Exception as e:
    check(f"unexpected exception type {type(e).__name__}", False)
finally:
    advisor.recommend_models = _orig_rec

print("-- adapters.call: sentinel consumed, model swapped, effort applied, call pinned --")
advisor.recommend_models = lambda *a, **k: {"top": [{"id": "openai:gpt-5-mini", "why": "x"}], "ranked_from": 2, "note": None}
captured = {}
_orig_g = adapters._call_guarded
def _stub(model, prompt, **kw):
    captured["model"] = model
    captured["reasoning"] = kw.get("reasoning")
    captured["no_sub"] = kw.get("_no_sub")
    return {"text": "ok", "cost": 0.004, "in_tok": 10, "out_tok": 5,
            "model": model.split(":", 1)[-1], "provider": model.split(":", 1)[0], "error": None, "executor": "api"}
adapters._call_guarded = _stub
try:
    with calls.context(intent=INTENT):
        r = adapters.call("openai:gpt-5.5", "hi", sig=INTENT, reasoning="best-value", max_tokens=100)
finally:
    adapters._call_guarded = _orig_g
    advisor.recommend_models = _orig_rec
check("model resolved to the advisor's pick", captured.get("model") == "openai:gpt-5-mini")
check("effort applied is the pick's learned effort, not the literal sentinel", captured.get("reasoning") == "minimal")
check("the chosen arm was PINNED (no_substitution)", captured.get("no_sub") is True)
check("counterfactual stamped: substituted_from = the named model", r.get("substituted_from") == "openai:gpt-5.5")
check("result flagged best_value", r.get("best_value") is True)

print(f"\n{'[FAIL]' if failures else 'OK'} test_best_value_select: {failures} failure(s)")
sys.exit(1 if failures else 0)
