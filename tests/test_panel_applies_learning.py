"""Guard — panels APPLY the learnings (per-member effort) WITHOUT collapsing diversity.

A panel member is a pinned call: vendor_call sets no_substitution=True on every member, so adapters.call resolves
reasoning="best-value" with pin_model=True — which titrates the EFFORT of the member's OWN model (from the learned
effort:<intent> fact) and never swaps the model. Applying the learning WITHIN each vendor slot (not as a global
"best model" argmax) is exactly what preserves cross-vendor diversity. Pins:
  · each member gets its own learned effort, and keeps its own model
  · a member with no learned effort falls through to its family floor (still not swapped)
  · end-to-end: two members stay two distinct vendors, each at its own learned effort
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

from spendguard import models, best_value, adapters, calls

failures = 0
def check(label, cond):
    global failures
    ok = bool(cond)
    if not ok:
        failures += 1
    print(f"  [{'OK' if ok else 'FAIL'}] {label}")


INTENT = "t:panel"
PANEL = ["openai:gpt-5.5", "anthropic:claude-opus-4-8"]
# Each vendor slot has its OWN learned cheapest-holding effort — different per model, as titration finds.
models.record_effort("openai:gpt-5.5", INTENT, "low")
models.record_effort("anthropic:claude-opus-4-8", INTENT, "medium")

print("-- each member titrates its OWN model's learned effort (pin_model=True), never swapped --")
picks = {m: best_value.select_model_effort(INTENT, m, pin_model=True) for m in PANEL}
check("gpt-5.5 member stays gpt-5.5 @ its learned 'low'",
      picks["openai:gpt-5.5"]["model"] == "openai:gpt-5.5" and picks["openai:gpt-5.5"]["effort"] == "low")
check("opus member stays opus @ its learned 'medium'",
      picks["anthropic:claude-opus-4-8"]["model"] == "anthropic:claude-opus-4-8" and picks["anthropic:claude-opus-4-8"]["effort"] == "medium")
check("every member kept its own vendor (diversity preserved)",
      {picks[m]["model"] for m in PANEL} == set(PANEL))

print("-- a member with no learned effort is not swapped either (family floor) --")
check("an unmeasured member resolves to None (keeps its model, family floor)",
      best_value.select_model_effort(INTENT, "zai:glm-5.2", pin_model=True)["model"] is None)

print("-- end-to-end: members stay distinct vendors, each at its own learned effort --")
seen = {}
_orig = adapters._call_guarded
def _stub(model, prompt, **kw):
    seen[model] = kw.get("reasoning")
    return {"text": "ok", "cost": 0.003, "in_tok": 8, "out_tok": 4,
            "model": model.split(":", 1)[-1], "provider": model.split(":", 1)[0], "error": None, "executor": "api"}
adapters._call_guarded = _stub
try:
    for m in PANEL:
        with calls.context(intent=INTENT):
            adapters.call(m, "review this", sig=INTENT, reasoning="best-value", no_substitution=True, max_tokens=100)
finally:
    adapters._call_guarded = _orig
check("both panel vendors were called (no collapse to one)", set(seen) == set(PANEL))
check("gpt-5.5 ran at its learned 'low'", seen.get("openai:gpt-5.5") == "low")
check("opus ran at its learned 'medium'", seen.get("anthropic:claude-opus-4-8") == "medium")
check("no member's effort leaked the literal sentinel", "best-value" not in set(seen.values()))

print(f"\n{'[FAIL]' if failures else 'OK'} test_panel_applies_learning: {failures} failure(s)")
sys.exit(1 if failures else 0)
