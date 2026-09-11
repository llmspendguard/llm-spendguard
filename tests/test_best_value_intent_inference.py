"""Guard — best_value INFERS the intent from the prompt when none is passed, so reasoning="best-value" still
applies instead of silently keeping the named model (Ash's ask: "if intent is not passed we can agentically use
the prompt to determine what it should be").

Pins:
  · no intent + a prompt → classify against the KNOWN recorded intents, then rank on that intent (model resolved);
  · the classifier may ONLY pick a KNOWN intent — a hallucinated/novel label is rejected → None → keep named model
    (only a known intent has the measured evidence recommend_models needs; a fresh label would have none);
  · no intent AND no prompt → no inference at all (honest degrade to the named model);
  · the inference is CACHED per prompt-hash (classifier called once per distinct prompt).
Offline: calls.recorded_intents, adapters.call (the classifier), advisor.recommend_models are stubbed — no spend.
"""
import os, sys, tempfile, json
if not os.environ.get("SPENDGUARD_TEST_ISOLATED"):
    os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"
    os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="spendguard-test-")
    _self = os.path.realpath(__file__)
    _root = os.path.realpath(os.path.dirname(__file__)) + os.sep
    if not _self.startswith(_root):
        raise SystemExit("refusing to re-exec a path outside the test directory: %s" % _self)
    os.execv(sys.executable, [sys.executable, _self])

from spendguard import best_value, calls, adapters, advisor, models

_fails = []
def check(label, cond):
    if not cond:
        _fails.append(label)
    print(f"  [{'OK' if cond else 'FAIL'}] {label}")


KNOWN = ["code-review", "loinc-typing"]
calls.recorded_intents = lambda **k: list(KNOWN)
models.effort_for = lambda m, i: None                       # family floor (isolate the model-pick behaviour)
advisor.recommend_models = lambda intent, k=1, quality_bar=None, run=False: {
    "top": [{"id": "openai:gpt-5-nano", "why": "cheapest that holds for %s" % intent}], "ranked_from": 2,
    "note": "ranked from evidence"}

_classify = {"intent": "code-review"}                        # what the classifier will return this scenario
_infer_calls = {"n": 0}
_orig_call = adapters.call
def _stub_call(model, prompt, sig=None, **kw):
    if sig == "spendguard:infer-intent":
        _infer_calls["n"] += 1
        j = {"intent": _classify["intent"]}
        return {"json": j, "text": json.dumps(j), "error": None, "cost": 0.001}
    return {"error": "unexpected sig %r" % sig, "text": None, "cost": 0.0}
adapters.call = _stub_call

try:
    best_value._intent_cache.clear()

    print("-- no intent + a prompt → classify to a KNOWN intent, then rank on it --")
    _classify["intent"] = "code-review"
    r = best_value.select_model_effort(None, "anthropic:claude-opus-4-8", prompt="Please review this diff for bugs")
    check("the classifier was consulted", _infer_calls["n"] == 1)
    check("best-value resolved a model off the inferred intent", r.get("model") == "openai:gpt-5-nano")
    check("the structured intent_inferred flag is set", r.get("considered", {}).get("intent_inferred") is True)

    print("-- the classifier may pick ONLY a known intent — a hallucinated label is rejected --")
    best_value._intent_cache.clear()
    _classify["intent"] = "some-brand-new-label"             # not in KNOWN
    r2 = best_value.select_model_effort(None, "anthropic:claude-opus-4-8", prompt="totally novel task text")
    check("a non-known label is NOT accepted → no model (keep the named one)", r2.get("model") is None)

    print("-- no intent AND no prompt → no inference, honest degrade --")
    _infer_calls["n"] = 0
    r3 = best_value.select_model_effort(None, "anthropic:claude-opus-4-8")
    check("classifier NOT called without a prompt", _infer_calls["n"] == 0)
    check("model is None (keep the named model)", r3.get("model") is None)

    print("-- inference is CACHED per prompt-hash (classifier called once per distinct prompt) --")
    best_value._intent_cache.clear()
    _classify["intent"] = "loinc-typing"
    _infer_calls["n"] = 0
    p = "Map this lab test to a LOINC code"
    best_value.select_model_effort(None, "anthropic:claude-opus-4-8", prompt=p)
    best_value.select_model_effort(None, "anthropic:claude-opus-4-8", prompt=p)
    check("classifier called ONCE for two identical prompts (cache hit)", _infer_calls["n"] == 1)
finally:
    adapters.call = _orig_call

print(f"\n{'[FAIL]' if _fails else 'OK'} test_best_value_intent_inference: {len(_fails)} failure(s)")
sys.exit(1 if _fails else 0)
