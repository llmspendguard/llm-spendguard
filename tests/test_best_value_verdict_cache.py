"""Guard — best-value caches the per-intent MODEL pick, invalidated by an evidence fingerprint, effort stays live.

best-value's model pick is a meta-caged advisor LLM call. Running the same intent many times should pay ~one pick,
not one per call. Pins:
  · a 2nd call with UNCHANGED evidence reuses the pick — the advisor is NOT called again (considered.cached=True);
  · when the evidence changes (calls.cost_summary moves the fingerprint) the pick IS re-derived (advisor called);
  · EFFORT is re-applied LIVE — a re-titration between two cached calls changes the returned effort (proves effort
    is never frozen into the cache);
  · a TRANSIENT advisor failure is NOT cached (the next call retries), and a DELIBERATE stop PROPAGATES.
Offline: advisor / calls / models are monkeypatched — no network, no spend."""
import os, sys, tempfile

if not os.environ.get("SPENDGUARD_TEST_ISOLATED"):
    os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"
    os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="spendguard-bvcache-")
    _self = os.path.realpath(__file__)
    _root = os.path.realpath(os.path.dirname(__file__)) + os.sep
    if not _self.startswith(_root):
        raise SystemExit("refusing to re-exec a path outside the test directory: %s" % _self)
    os.execv(sys.executable, [sys.executable, _self])

from spendguard import best_value, advisor, calls, models, gate

_fails = []
def ck(label, cond):
    if not cond:
        _fails.append(label)
    print(f"  [{'OK' if cond else 'FAIL'}] {label}")

# ── controllable stand-ins ──
_calls = {"n": 0}
_evidence = {"rows": [("myintent", "openai:gpt-5.5", 10, 1.0, 8, 2)]}   # (intent, model, jobs, $, good, bad)
_effort = {"v": "low"}

def _fake_recommend(intent, k=5, quality_bar=None, run=False):
    _calls["n"] += 1
    return {"top": [{"id": "openai:gpt-5-nano", "why": "cheapest that held quality"}], "ranked_from": 3, "note": "ok"}

advisor.recommend_models = _fake_recommend
calls.cost_summary = lambda intent=None: list(_evidence["rows"])
models.effort_for = lambda model, intent: _effort["v"]

print("-- 1st pick calls the advisor; 2nd (unchanged evidence) reuses it, NO advisor call --")
best_value._verdict_cache.clear()
r1 = best_value.select_model_effort("myintent", "openai:gpt-5.5")
ck("1st call picked the advisor's model", r1["model"] == "openai:gpt-5-nano")
ck("advisor called once", _calls["n"] == 1)
ck("1st call not marked cached", r1["considered"].get("cached") is False)
r2 = best_value.select_model_effort("myintent", "openai:gpt-5.5")
ck("2nd call same pick", r2["model"] == "openai:gpt-5-nano")
ck("advisor NOT called again (served from cache)", _calls["n"] == 1)
ck("2nd call marked cached", r2["considered"].get("cached") is True)

print("-- EFFORT is re-applied LIVE (a re-titration lands without an advisor call) --")
_effort["v"] = "high"
r3 = best_value.select_model_effort("myintent", "openai:gpt-5.5")
ck("still served from cache (advisor not called)", _calls["n"] == 1)
ck("but the effort reflects the NEW titration (not frozen in the cache)", r3["effort"] == "high")

print("-- changed EVIDENCE moves the fingerprint → the pick is re-derived --")
_evidence["rows"] = [("myintent", "openai:gpt-5.5", 25, 3.0, 20, 5)]    # a new judged sample / more jobs
r4 = best_value.select_model_effort("myintent", "openai:gpt-5.5")
ck("advisor called again after the evidence changed", _calls["n"] == 2)
ck("re-derived pick is fresh (not cached)", r4["considered"].get("cached") is False)

print("-- a TRANSIENT advisor failure is NOT cached (next call retries) --")
best_value._verdict_cache.clear()
_evidence["rows"] = [("intentB", "m", 1, 0.1, 1, 0)]
def _boom(*a, **k):
    _calls["n"] += 1
    raise RuntimeError("advisor down")
advisor.recommend_models = _boom
_before = _calls["n"]
rb1 = best_value.select_model_effort("intentB", "openai:gpt-5.5")
rb2 = best_value.select_model_effort("intentB", "openai:gpt-5.5")
ck("failure returns no-pick (keeps named model)", rb1["model"] is None and rb2["model"] is None)
ck("failure NOT cached — advisor retried on the 2nd call", _calls["n"] == _before + 2)

print("-- a DELIBERATE stop PROPAGATES (never swallowed, never cached) --")
def _refuse(*a, **k):
    raise gate.SpendGateRefused.__new__(gate.SpendGateRefused)
advisor.recommend_models = _refuse
try:
    best_value.select_model_effort("intentC", "openai:gpt-5.5")
    ck("deliberate stop raised", False)
except gate.SpendGateRefused:
    ck("deliberate stop raised (SpendGateRefused)", True)

print(f"\n{'[FAIL]' if _fails else 'OK'} test_best_value_verdict_cache: {len(_fails)} failure(s)")
sys.exit(1 if _fails else 0)
