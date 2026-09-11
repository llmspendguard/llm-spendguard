"""Phase 4 guard — the per-DECISION value proof: one record serves both pillars, on its own axis.

A substitution books ONE decision row (intent, requested→chosen model+effort, counterfactual $, actual $, saved $)
and — only for a metered→cheaper-metered swap — a guarded saving. Pins:
  · _book_substitution prices the counterfactual off r['substituted_from'] (the model the caller WOULD have run)
  · a metered→cheaper swap records saved = baseline − actual, booked under the 'best-value' source
  · a $0 plan swap records the DECISION (counterfactual visible) but saved=0 and NO savings-ledger row (est-value)
  · Σ decisions.saved_usd == the savings-ledger 'best-value' total (the two never drift)
  · saved is a THIRD axis in spend_overview — separate keys, never summed into real-$ or est-value
  · end-to-end: a best-value call through adapters.call leaves a decision row with requested→chosen provenance
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

from spendguard import guard, adapters, pricing, calls, mcp_server

failures = 0
def check(label, cond):
    global failures
    ok = bool(cond)
    if not ok:
        failures += 1
    print(f"  [{'OK' if ok else 'FAIL'}] {label}")


def _saved(source):
    return round(sum(r["k1"] for r in guard.by_dims_guarded() if r["source"] == source), 6)


print("-- a metered→cheaper best-value swap books a decision + a 'best-value' saving --")
_orig_rt = pricing.realtime_cost
pricing.realtime_cost = lambda m, i, o: 0.10 if m == "openai:gpt-5.5" else 0.02   # baseline (requested) = $0.10
adapters._book_substitution({"substituted_from": "openai:gpt-5.5", "cost": 0.02, "in_tok": 100, "out_tok": 50,
                             "provider": "openai", "model": "gpt-5-mini", "best_value": True,
                             "requested_effort": None, "chosen_effort": "low"})
check("a 'best-value' saving of 0.08 (0.10 baseline − 0.02 actual) was booked", _saved("best-value") == 0.08)
rows = guard.decisions_since()
check("one decision row recorded", len(rows) == 1)
d = rows[0] if rows else {}
check("decision priced the counterfactual off the REQUESTED model", d.get("counterfactual_usd") == 0.10)
check("decision recorded the chosen model + effort", d.get("chosen_model") == "openai:gpt-5-mini" and d.get("chosen_effort") == "low")
check("decision saved_usd = 0.08", round(d.get("saved_usd") or 0, 6) == 0.08)
check("decision basis is best-value", d.get("basis") == "best-value")

print("-- a $0 plan swap records the decision but books NO saving (est-value axis) --")
_before = _saved("best-value")
adapters._book_substitution({"substituted_from": "openai:gpt-5.5", "cost": 0.0, "in_tok": 100, "out_tok": 50,
                             "provider": "codex", "model": "gpt-5.5", "best_value": True})
pricing.realtime_cost = _orig_rt
check("no additional 'best-value' saving from the $0 swap", _saved("best-value") == _before)
rows2 = guard.decisions_since()
check("the $0 swap still recorded a decision (2 total)", len(rows2) == 2)
check("the $0 swap decision shows counterfactual>0 but saved=0",
      any(r.get("actual_usd") == 0.0 and (r.get("counterfactual_usd") or 0) > 0 and (r.get("saved_usd") or 0) == 0 for r in rows2))

print("-- Σ decisions.saved_usd == the savings-ledger 'best-value' total (no drift) --")
summ = guard.decisions_summary()
check("decisions_summary counts both decisions", summ["decisions"] == 2)
check("Σ decisions.saved_usd == booked best-value saving", round(summ["saved_usd"], 6) == _saved("best-value") == 0.08)

print("-- saved is its own axis (counterfactual, not certain); the tally never blurs them --")
s = guard.saved_since()
check("best-value counts as COUNTERFACTUAL, not certain",
      round(s["counterfactual"], 6) == 0.08 and s["certain"] == 0.0)

print("-- spend_overview exposes saved as a SEPARATE third axis (never summed) --")
ov = mcp_server._tool_spend_overview({})
check("overview has a distinct saved_usd axis", "saved_usd" in ov and "real_usd" in ov and "est_value_usd" in ov)
check("saved is NOT folded into total_real", ov["saved_usd"]["total"] == 0.08 and "saved" not in str(ov["real_usd"]))
check("the note says three axes are never summed", "never summed" in ov["note"].lower())

print("-- end-to-end: a best-value call through adapters.call leaves a decision row --")
from spendguard import advisor, models
INTENT = "t:decide"
# best_value now picks the MODEL agentically (advisor.recommend_models) and the EFFORT from the titration fact.
models.record_effort("openai:gpt-5-mini", INTENT, "low")
_orig_rec = advisor.recommend_models
advisor.recommend_models = lambda *a, **k: {"top": [{"id": "openai:gpt-5-mini", "why": "cheapest that holds"}],
                                            "ranked_from": 2, "note": None}
_before_n = len(guard.decisions_since())
_orig_g = adapters._call_guarded
def _stub(model, prompt, **kw):
    prov, bare = model.split(":", 1) if ":" in model else ("openai", model)
    return {"text": "ok", "cost": 0.009, "in_tok": 50, "out_tok": 20, "provider": prov, "model": bare,
            "error": None, "executor": "api"}
adapters._call_guarded = _stub
try:
    with calls.context(intent=INTENT):
        adapters.call("openai:gpt-5.5", "go", sig=INTENT, reasoning="best-value", max_tokens=100)
finally:
    adapters._call_guarded = _orig_g
    advisor.recommend_models = _orig_rec
_after = guard.decisions_since()
check("a decision row was added by the call-path best-value substitution", len(_after) == _before_n + 1)
# Find it by intent — same-second timestamps make ts-DESC order among sibling rows non-deterministic.
_new = [r for r in _after if r.get("intent") == INTENT]
check("exactly one decision for this intent", len(_new) == 1)
check("the new decision names the requested→chosen provenance",
      bool(_new) and _new[0].get("requested_model") == "openai:gpt-5.5"
      and _new[0].get("chosen_model") == "openai:gpt-5-mini")

print(f"\n{'[FAIL]' if failures else 'OK'} test_decision_savings: {failures} failure(s)")
sys.exit(1 if failures else 0)
