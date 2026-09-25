"""Guard — estimate_panel prices with the PROVIDER (the full 'provider:model'), never the stripped bare name. A bare
model id several vendors publish (e.g. deepseek-v4-flash — 9 vendors) is AMBIGUOUS: stripping the provider made
realtime_cost raise → silently read as 'unpriced' → the whole estimate INDETERMINATE (the deepseek incident that
blocked a live panel). And an unpriceable model records WHY per row (price_error), never swallowed to a bare None
(honestreview I1 silent-drop). pricing is STUBBED so the guard tests the FIX itself and is host-independent (it does not
depend on which models the local catalogue happens to hold). Offline: no network, no model call."""
import os
import sys
import tempfile

os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="sg-panelprice-")
os.environ.setdefault("SPENDGUARD_TEST_ISOLATED", "1")
os.environ.setdefault("SPENDGUARD_NO_AUTOINSTALL", "1")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                "scripts", "integration", "conformance"))

from spendguard import pricing  # noqa: E402
import panel_review as PR  # noqa: E402

_fails = []
def ck(label, cond):
    if not cond:
        _fails.append(label)
    print(f"  [{'OK' if cond else 'FAIL'}] {label}")

# Stub pricing: RECORD the exact model string estimate_panel prices with. A provider-qualified id is priced; a bare
# ambiguous id raises (as the real resolver does); an unknown id raises. This isolates the FIX (provider passed, not
# stripped) from the host's catalogue.
_seen = []
def _fake_realtime_cost(model, in_tok, out_tok=0, **kw):
    _seen.append(model)
    if model == "anthropic:claude-opus-4-8":
        return 0.02
    if model == "deepseek:deepseek-v4-flash":
        return 0.01
    if model == "deepseek-v4-flash":                 # the BARE name the old code stripped to → ambiguous, raises
        raise KeyError("Ambiguous price for 'deepseek-v4-flash': pass the provider")
    raise KeyError(f"No canonical price for {model!r}")

_real = pricing.realtime_cost
pricing.realtime_cost = _fake_realtime_cost
try:
    print("-- estimate_panel prices with the full provider:model, never the stripped bare name --")
    est = PR.estimate_panel(models=["anthropic:claude-opus-4-8", "deepseek:deepseek-v4-flash"], budget_usd=99.0)
    ck("realtime_cost was called with the FULL 'provider:model' (deepseek:deepseek-v4-flash)",
       "deepseek:deepseek-v4-flash" in _seen)
    ck("the provider was NOT stripped to the ambiguous bare 'deepseek-v4-flash'", "deepseek-v4-flash" not in _seen)
    ck("the provider-qualified reviewer is PRICED (not a false 'unpriced' gap)",
       "deepseek:deepseek-v4-flash" not in est["unpriced"])
    ck("the estimate is COMPLETE and within budget", est["within_budget"] is True and est["unpriced"] == [])

    print("-- an unpriceable model records WHY (no silent drop) --")
    est2 = PR.estimate_panel(models=["novendor:no-such-model"], budget_usd=99.0)
    ck("an unknown model lands in unpriced (a NAMED gap)", "novendor:no-such-model" in est2["unpriced"])
    ck("its row records WHY it is unpriced (price_error captured, not a swallowed None)",
       any(r["model"] == "novendor:no-such-model" and r.get("price_error") for r in est2["rows"]))
    ck("within_budget is False on an incomplete (unpriced) estimate", est2["within_budget"] is False)
finally:
    pricing.realtime_cost = _real

print(f"\n{'[FAIL]' if _fails else 'OK'} test_panel_estimate_provider_pricing: {len(_fails)} failure(s)")
sys.exit(1 if _fails else 0)
