"""A PROACTIVE bandit/lane substitution that FAILS must fall back to the ORIGINAL model in-call, not strand the
caller (7thsense 2026-09-25): adapters.call("gpt-5.4-nano", …) with no no_substitution was bandit-routed to the
claude-code lane (opus); the opus substitute Connection-errored and its result was returned as a hard failure, while
the original nano (working OpenAI key) was never tried — even though the same recovery already works for a "lane
missed" outcome (the reactive path). This guards the proactive path: substitute fails → shed its arm → run the
original. AND it guards that a DELIBERATE spend-stop (SpendGateRefused) from the substitute still PROPAGATES (it must
never be swallowed into a substitute-error and then continued on the original). Offline: adapters.call is stubbed."""
import os
import sys
import tempfile

os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="sg-subfail-")
os.environ.setdefault("SPENDGUARD_TEST_ISOLATED", "1")
os.environ.setdefault("SPENDGUARD_NO_AUTOINSTALL", "1")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from spendguard import adapters, lane_balance  # noqa: E402
from spendguard.gate import SpendGateRefused  # noqa: E402

_fails = []
def ck(label, cond):
    if not cond:
        _fails.append(label)
    print(f"  [{'OK' if cond else 'FAIL'}] {label}")


ORIG = "openai:gpt-5.4-nano"
SUB = "anthropic:claude-opus-4-8"

# force a proactive substitution ORIG → SUB (the bandit pick), only on the proactive (non-reactive) call
_real_rd = lane_balance.route_decision
def _fake_rd(intent, model, reactive=False):
    if not reactive and "nano" in (model or ""):
        return SUB, "bandit → claude-code (claude-opus-4-8)"
    return None, "none"


# a lane so the shed can resolve provider_for(SUB) → 'anthropic' → ('claude-code', …)
_real_lanes = adapters._LANES
_real_input_fits = adapters._input_fits
_real_cool = adapters._lane_model_cool
_cooled = []
def _fake_cool(lane, model):
    _cooled.append((lane, model))


def _run(scenario):
    """scenario decides how the SUBSTITUTE behaves; the ORIGINAL always succeeds."""
    _seen = []

    def _fake_call(model, prompt, **kw):
        _seen.append(model)
        if "opus" in model:                      # the substitute
            if scenario == "connection":
                return {"provider": "anthropic", "model": model, "text": None, "error": "Connection error.",
                        "error_type": "APIConnectionError", "cost": None, "in_tok": 0, "out_tok": 0,
                        "latency": 0.0, "finish_reason": None, "truncated": None}
            if scenario == "refused":
                raise SpendGateRefused("budget cap reached — stop spending")
        return {"provider": "openai", "model": model, "text": "OK", "error": None, "cost": 0.001,
                "in_tok": 10, "out_tok": 2, "latency": 0.1, "finish_reason": "stop", "truncated": False}

    adapters.call = _fake_call
    try:
        return adapters._call_guarded(ORIG, "a data-heavy WHO GHO prompt", sig="7thsense:comprehend-asset-group"), _seen
    finally:
        adapters.call = _real_call


_real_call = adapters.call
lane_balance.route_decision = _fake_rd
adapters._LANES = {**_real_lanes, "anthropic": ("claude-code", "subscription_exec")}
adapters._input_fits = lambda *a, **k: (True, "")
adapters._lane_model_cool = _fake_cool
try:
    print("-- (1) substitute Connection-errors → fall back to the ORIGINAL, shed the arm --")
    r, seen = _run("connection")
    ck("caller got a RESULT, not the substitute's error", r.get("error") is None and r.get("text") == "OK")
    ck("the ORIGINAL model ran (fell back to nano)", "nano" in (r.get("model") or ""))
    ck("the substitute was attempted first, then the original ran", any("opus" in m for m in seen) and any("nano" in m for m in seen))
    ck("the failed (lane, model) arm was shed/cooled", any("opus" in m for _, m in _cooled))
    ck("a failed substitute books NO false saving (no substituted_from on the fallback)", "substituted_from" not in r)

    print("-- (2) substitute raises a DELIBERATE stop (SpendGateRefused) → it PROPAGATES, never continues on the original --")
    _cooled.clear()
    raised = None
    try:
        _run("refused")
    except SpendGateRefused as e:
        raised = e
    ck("SpendGateRefused propagated (spend stop honored, not swallowed)", isinstance(raised, SpendGateRefused))
finally:
    adapters.call = _real_call
    lane_balance.route_decision = _real_rd
    adapters._LANES = _real_lanes
    adapters._input_fits = _real_input_fits
    adapters._lane_model_cool = _real_cool

print(f"\n{'[FAIL]' if _fails else 'OK'} test_substitute_failure_fallback: {len(_fails)} failure(s)")
sys.exit(1 if _fails else 0)
