"""Guardrail D — budget_usd is a REAL RUNNING CAP, not an upfront estimate-gate. A fan tracks the ACTUAL cost of each
settled result and STOPS SUBMITTING once the cap is committed, returning what completed + a NAMED budget_exhausted row
for the remainder — never spending to N. This is the backstop that makes the gpt-5.5 15x overspend impossible even when
the estimate is wrong: the cap is truth (actual cost), not a projection. It never cancels an in-flight call (a completed
request still bills). Acceptance test from docs/GUARDRAILS_reasoning_overspend.md §4.

Hermetic: adapters.call + dispatch stubbed to a fixed $0.10/call; no network."""
import os
import sys
import tempfile

os.environ.setdefault("SPENDGUARD_TEST_ISOLATED", "1")
os.environ.setdefault("SPENDGUARD_HOME", tempfile.mkdtemp(prefix="spendguard-cap-"))
os.environ["SPENDGUARD_ROUTE_THROUGH_QUEUE"] = "0"

from spendguard import adapters, dispatch, lane_balance

_fails = []
def ck(label, cond):
    if not cond:
        _fails.append(label)
    print(f"  [{'OK' if cond else 'FAIL'}] {label}")

_PER_CALL = 0.10
_calls = []
_saved_call, _saved_acq, _saved_rel = adapters.call, dispatch.acquire_or_none, dispatch.release
def _stub_call(model, prompt, **kw):
    _calls.append(prompt)
    return {"text": "ok", "model": model, "provider": "openai", "cost": _PER_CALL, "in_tok": 1, "out_tok": 1,
            "error": None, "executor": "api"}
adapters.call = _stub_call
dispatch.acquire_or_none = lambda *a, **k: object()
dispatch.release = lambda *a, **k: None

try:
    # 20 tasks at $0.10, cap $0.50, chunk_size=1 (tight) → ~5 issued ($0.50), the rest UNISSUED
    print("-- 20 tasks @ $0.10, budget $0.50 → stops at ≈cap, remainder unissued --")
    res = lane_balance.bulk_delegate([{"id": i} for i in range(20)], "cap:test",
                                     model_for=lambda t: "openai:gpt-x", budget_usd=0.50, chunk_size=1, force=True)
    issued = [r for r in res if (r or {}).get("text")]
    unissued = [r for r in res if (r or {}).get("reason") == "budget_exhausted"]
    ck("stopped at ≈cap: about 5 tasks issued (5 × $0.10 = $0.50)", 5 <= len(issued) <= 6)
    ck("the remainder is UNISSUED (named budget_exhausted), not spent to N", len(unissued) >= 13)
    ck("issued + unissued account for ALL tasks (honest short, never a silent drop)",
       len(issued) + len(unissued) == 20)
    ck("only ≈cap worth of calls were actually MADE (not 20)", len(_calls) <= 6)
    ck("an unissued row carries a typed reason + the cap in its error", unissued and "budget" in (unissued[0].get("error") or "").lower())

    # no budget → unchanged: every task runs
    print("-- no budget_usd → the cap is off (every task runs, backward compatible) --")
    _calls.clear()
    res2 = lane_balance.bulk_delegate([{"id": i} for i in range(8)], "cap:test",
                                      model_for=lambda t: "openai:gpt-x", chunk_size=1, force=True)
    ck("no cap → all 8 tasks issued", sum(1 for r in res2 if (r or {}).get("text")) == 8 and len(_calls) == 8)
finally:
    adapters.call, dispatch.acquire_or_none, dispatch.release = _saved_call, _saved_acq, _saved_rel

print(f"\n{'[FAIL]' if _fails else 'OK'} test_budget_running_cap: {len(_fails)} failure(s)")
sys.exit(1 if _fails else 0)
