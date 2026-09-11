"""Guard — reasoning-effort auto-titration per (intent, model): the effort twin of the token-budget self-heal.

Pins the whole loop:
  · models.record_effort / effort_for round-trip (the per-(intent,model) fact store, mirroring mark_ineffective)
  · the CHOKEPOINT auto-apply: a call with a titrated intent and NO explicit effort gets the learned effort; an
    explicit effort still WINS; an intent with no fact falls through to the family floor (no forced effort)
  · titrate(): A/Bs the effort ladder, scores 1-10 (graded), takes the AGENTIC verdict, records the cheapest
    holding effort as the effort:<intent> fact, and checkpoints (resumable) — estimate-first
"""
import os, sys, tempfile
if not os.environ.get("SPENDGUARD_TEST_ISOLATED"):
    os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"
    os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="spendguard-test-")
    _self = os.path.realpath(__file__)                    # contain the spawn: re-exec THIS file, validated under tests/
    _root = os.path.realpath(os.path.dirname(__file__)) + os.sep
    if not _self.startswith(_root):
        raise SystemExit("refusing to re-exec a path outside the test directory: %s" % _self)
    os.execv(sys.executable, [sys.executable, _self])

from spendguard import models, adapters, calls, callio, effort_titration

failures = 0
def check(label, cond):
    global failures
    ok = bool(cond)
    if not ok:
        failures += 1
    print(f"  [{'OK' if ok else 'FAIL'}] {label}")


print("-- models.record_effort / effort_for round-trip --")
models.record_effort("openai:gpt-5.5", "t:classify", "low")
check("effort_for returns the recorded effort", models.effort_for("openai:gpt-5.5", "t:classify") == "low")
check("a different intent has no effort fact (family floor)", models.effort_for("openai:gpt-5.5", "t:other") is None)
check("a global effort fact backstops when no intent fact", (
    models.record_effort("openai:gpt-5-mini", None, "minimal") or
    models.effort_for("openai:gpt-5-mini", "t:anything") == "minimal"))

print("-- chokepoint auto-apply: learned effort applied when the caller sets none; explicit wins --")
seen = {}
_orig_once = adapters._call_once
def _stub_once(model, prompt, max_tokens=None, system=None, reasoning=None, **kw):
    seen["reasoning"] = reasoning
    return {"provider": "openai", "model": model.split(":", 1)[-1], "text": "ok", "in_tok": 5, "out_tok": 3,
            "cost": 0.001, "latency": 0.0, "finish_reason": "stop", "error": None, "executor": "api"}
adapters._call_once = _stub_once
try:
    adapters.call("openai:gpt-5.5", "hi", sig="t:classify", max_tokens=100)   # no explicit effort
    check("the learned effort 'low' was auto-applied at the chokepoint", seen.get("reasoning") == "low")
    seen.clear()
    adapters.call("openai:gpt-5.5", "hi", sig="t:classify", reasoning="high", max_tokens=100)  # explicit
    check("an explicit effort WINS over the learned fact", seen.get("reasoning") == "high")
    seen.clear()
    adapters.call("openai:gpt-5.5", "hi", sig="t:unmeasured", max_tokens=100)  # no fact for this intent
    check("an unmeasured intent applies NO effort (family floor stands)", seen.get("reasoning") is None)
finally:
    adapters._call_once = _orig_once

print("-- titrate(): A/B the ladder, graded score, agentic verdict, record the effort:<intent> fact --")
INTENT = "t:extract"
MODEL = "openai:gpt-5.5"
for i in range(6):                                        # seed the intent's prompt sample (what titrate replays)
    callio.record_io_sample(INTENT, "openai", "gpt-5.5", "b1", "c%d" % i, "classify row %d" % i, "out %d" % i)

_orig_call = adapters.call
def _stub_call(model, prompt, **kw):
    sigv = kw.get("sig")
    if sigv == "spendguard:effort-score":                 # graded judge — canned 1-10 score
        return {"text": '{"score": 8, "usable": true}', "json": {"score": 8, "usable": True}, "cost": 0.0,
                "in_tok": 5, "out_tok": 3, "error": None, "executor": "api"}
    if sigv == "spendguard:effort-verdict":               # agentic verdict — picks the cheapest holding effort
        return {"text": '{"effort":"low","quality_score":8,"confident":true,"why":"low holds"}',
                "json": {"effort": "low", "quality_score": 8, "confident": True, "why": "low holds"},
                "cost": 0.0, "in_tok": 20, "out_tok": 12, "error": None, "executor": "api"}
    eff = kw.get("reasoning")                             # an A/B run at some effort — cheaper at low
    return {"text": "ans@%s" % eff, "cost": {"low": 0.01, "high": 0.05}.get(eff, 0.03), "in_tok": 20,
            "out_tok": 30, "error": None, "model": model.split(":", 1)[-1], "executor": "api"}
adapters.call = _stub_call
try:
    est = effort_titration.titrate(INTENT, model=MODEL, efforts=["low", "high"], run=False)
    check("estimate-first: run=False spends nothing + returns an estimate", est.get("estimate_only") and est.get("estimate_usd") is not None)
    check("no fact recorded during the estimate", models.effort_for(MODEL, INTENT) is None)

    r = effort_titration.titrate(INTENT, model=MODEL, efforts=["low", "high"], run=True, budget_usd=100.0)
    check("titrate recorded a verdict", isinstance(r.get("verdict"), dict) and r["verdict"].get("effort") == "low")
    check("the cheapest holding effort was recorded as the effort:<intent> fact", models.effort_for(MODEL, INTENT) == "low")
    check("both efforts were A/B'd", set(r.get("per_effort", {})) == {"low", "high"})
    check("the confident verdict stopped early at the pilot", r.get("sampled") == effort_titration._PILOT)
finally:
    adapters.call = _orig_call

print("-- the titrated fact now auto-applies to a plain call of that intent --")
seen.clear()
adapters._call_once = _stub_once
try:
    adapters.call(MODEL, "go", sig=INTENT, max_tokens=100)
    check("a plain call of the titrated intent runs at the learned 'low'", seen.get("reasoning") == "low")
finally:
    adapters._call_once = _orig_once

print(f"\n{'[FAIL]' if failures else 'OK'} test_effort_titration: {failures} failure(s)")
sys.exit(1 if failures else 0)
