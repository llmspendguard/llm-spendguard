"""Cost-aware bulk fallback (Warden #1 part 1): a BULK lane-miss on an EXPENSIVE arm (opus/sol) must NOT silently bill
that model's metered API — the $0 lane is free, the runaway is the paid fallback (measured: warden:describe → codex /
claude-code lane miss → metered gpt-5.6-sol / claude-opus-4-8). Unless the caller opted into paid (budget_usd caps it,
metered_only wants it, or model_for pins the model as the measurement), an expensive arm's miss becomes a $0 error row
(retried on a cheaper lane), exactly like refuse_billed. A cheap arm (gemini/glm/luna) is unaffected. Offline: the arm
selection, dispatch governor, and adapters.call are stubbed — no network, no threads-that-need-real-lanes, no spend."""
import os
import sys
import tempfile

os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="sg-bulkcost-")
os.environ.setdefault("SPENDGUARD_TEST_ISOLATED", "1")
os.environ.setdefault("SPENDGUARD_NO_AUTOINSTALL", "1")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from spendguard import lane_balance as lb  # noqa: E402

_fails = []
def ck(label, cond):
    if not cond:
        _fails.append(label)
    print(f"  [{'OK' if cond else 'FAIL'}] {label}")


print("-- (1) _arm_fallback_pricey classifies the runaway culprits, spares the cheap lanes --")
ck("codex → gpt-5.6-sol is pricey", lb._arm_fallback_pricey("codex", "gpt-5.6-sol") is True)
ck("claude-code → claude-opus-4-8 is pricey", lb._arm_fallback_pricey("claude-code", "claude-opus-4-8") is True)
ck("gemini → gemini-3.8-flash-low is NOT pricey", lb._arm_fallback_pricey("gemini", "gemini-3.8-flash-low") is False)
ck("zai-coding → glm-5.3 is NOT pricey", lb._arm_fallback_pricey("zai-coding", "glm-5.3") is False)
ck("codex → gpt-5.6-luna (the CHEAP codex variant) is NOT pricey", lb._arm_fallback_pricey("codex", "gpt-5.6-luna") is False)

print("-- (2) a bulk fan on an EXPENSIVE arm sheds the paid fallback (no opt-in) — the item errors $0, not billed --")
# stub the arm set to ONE expensive arm, the governor to always grant, and adapters.call to CAPTURE no_metered_fallback
_seen = []
_real_arms, _real_call = lb._bulk_arms, lb.adapters.call if hasattr(lb, "adapters") else None
from spendguard import adapters, dispatch  # noqa: E402
_real_call = adapters.call
_real_acq, _real_rel = dispatch.acquire_or_none, dispatch.release


def _fake_call(model, prompt, **kw):
    _seen.append({"model": model, "no_metered_fallback": kw.get("no_metered_fallback")})
    return {"provider": "openai", "model": model.split(":", 1)[-1], "text": "ok", "error": None, "cost": 0.0,
            "in_tok": 5, "out_tok": 3, "latency": 0.1, "executor": "codex", "finish_reason": "stop"}


def _run_bulk(**bulk_kw):
    _seen.clear()
    lb._bulk_arms = lambda intent, lanes=None: [("codex", "gpt-5.6-sol")]
    adapters.call = _fake_call
    dispatch.acquire_or_none = lambda *a, **k: 0.0     # a granted slot (truthy-or-0 → not None)
    dispatch.release = lambda *a, **k: None
    try:
        lb.bulk_delegate(["one task"], intent="warden:describe", deadline_s=5.0, max_workers=1, **bulk_kw)
    finally:
        lb._bulk_arms, adapters.call = _real_arms, _real_call
        dispatch.acquire_or_none, dispatch.release = _real_acq, _real_rel
    return _seen[0] if _seen else None


row = _run_bulk()
ck("no opt-in: the expensive arm's call ran with no_metered_fallback=True (paid fallback shed)",
   row is not None and row["no_metered_fallback"] is True)

row_budget = _run_bulk(budget_usd=1.0)
ck("budget_usd set (paid opt-in, capped): the expensive fallback is ALLOWED (no_metered_fallback=False)",
   row_budget is not None and row_budget["no_metered_fallback"] is False)

row_pinned = _run_bulk(model_for=lambda t: "openai:gpt-5.6-sol")
ck("model_for pinned (the model IS the measurement): the fallback is ALLOWED",
   row_pinned is not None and row_pinned["no_metered_fallback"] is False)

print(f"\n{'[FAIL]' if _fails else 'OK'} test_bulk_cost_aware_fallback: {len(_fails)} failure(s)")
sys.exit(1 if _fails else 0)
