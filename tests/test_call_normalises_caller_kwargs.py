"""Guard — adapters.call NORMALISES the kwargs a caller naturally reaches for, instead of a cryptic TypeError
(the "users try to use it and fail" class, caught by a live cross-provider run: a caller wrote
  adapters.call(model, prompt, reasoning="best-value", intent="…")
and got `TypeError: call() got an unexpected keyword argument 'intent'`).

Pins the ensure-success contract:
  · intent= is accepted (alias for sig): does NOT raise, aliases to sig, and best-value resolves the intent FROM it;
  · when BOTH sig and intent are given, best-value prefers the explicit intent (sig stays the finer call-class);
  · effort= / reasoning_effort= alias reasoning=; max_output_tokens= / max_completion_tokens= alias max_tokens=;
  · the canonical param WINS when both it and its alias are passed (the alias is only a fallback);
  · a genuinely unknown kwarg fails LOUDLY with guidance (names the accepted params) — never silently swallowed.
Offline: _call_guarded + best_value.select_model_effort are stubbed — no network, no spend.
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

from spendguard import adapters, best_value

_fails = []
def check(label, cond):
    if not cond:
        _fails.append(label)
    print(f"  [{'OK' if cond else 'FAIL'}] {label}")


_seen = {}
_orig_guarded = adapters._call_guarded
def _stub_guarded(model, prompt, max_tokens=None, system=None, reasoning=None, schema=None, timeout_s=None,
                  sig=None, **kw):
    # capture what reached the guarded layer AFTER normalisation + best-value swap (sig is threaded here, not to _call_once)
    _seen.update(model=model, sig=sig, reasoning=reasoning, max_tokens=max_tokens)
    prov = model.split(":", 1)[0] if ":" in model else "openai"
    return {"provider": prov, "model": model.split(":", 1)[-1], "text": "ok", "in_tok": 5, "out_tok": 3,
            "cost": 0.001, "latency": 0.0, "finish_reason": "stop", "error": None, "executor": "api"}
adapters._call_guarded = _stub_guarded

_bv_calls = []
_orig_sel = best_value.select_model_effort
def _stub_select(intent, requested_model, pin_model=False, **kw):
    _bv_calls.append(intent)
    return {"model": "openai:gpt-5-nano", "effort": "low", "why": f"best-value: {intent}"}
best_value.select_model_effort = _stub_select

try:
    print("-- intent= does not raise, aliases to sig (attribution/measurement see it) --")
    raised = None
    try:
        adapters.call("openai:gpt-5.5", "hi", max_tokens=50, intent="t:intentalias")
    except TypeError as e:
        raised = str(e)
    check("call(intent=…) did NOT raise TypeError", raised is None)
    check("intent aliased to sig at the guarded layer", _seen.get("sig") == "t:intentalias")

    print("-- reasoning='best-value' resolves the intent FROM THE PARAM --")
    _bv_calls.clear()
    adapters.call("openai:gpt-5.5", "hi", max_tokens=50, reasoning="best-value", intent="t:bvintent")
    check("best_value.select_model_effort got the param intent", _bv_calls == ["t:bvintent"])
    check("the sentinel was consumed (no literal 'best-value' on the wire)", _seen.get("reasoning") != "best-value")

    print("-- both sig and intent given → best-value prefers the explicit intent --")
    _bv_calls.clear()
    adapters.call("openai:gpt-5.5", "hi", max_tokens=50, reasoning="best-value", sig="finer", intent="t:jobtype")
    check("best-value used the explicit intent, not the finer sig", _bv_calls == ["t:jobtype"])

    print("-- effort= / reasoning_effort= alias reasoning=; canonical wins when both given --")
    adapters.call("openai:gpt-5.5", "hi", max_tokens=50, effort="high")
    check("effort= aliased to reasoning=", _seen.get("reasoning") == "high")
    adapters.call("openai:gpt-5.5", "hi", max_tokens=50, reasoning_effort="medium")
    check("reasoning_effort= aliased to reasoning=", _seen.get("reasoning") == "medium")
    adapters.call("openai:gpt-5.5", "hi", max_tokens=50, reasoning="low", effort="high")
    check("explicit reasoning= WINS over the effort= alias", _seen.get("reasoning") == "low")

    print("-- max_output_tokens= / max_completion_tokens= alias max_tokens= --")
    adapters.call("openai:gpt-5.5", "hi", max_output_tokens=1234)
    check("max_output_tokens= aliased to max_tokens=", _seen.get("max_tokens") == 1234)
    adapters.call("openai:gpt-5.5", "hi", max_completion_tokens=777)
    check("max_completion_tokens= aliased to max_tokens=", _seen.get("max_tokens") == 777)

    print("-- an unknown kwarg fails LOUDLY with guidance (never silently swallowed) --")
    msg = None
    try:
        adapters.call("openai:gpt-5.5", "hi", max_tokens=50, temperature=0)
    except TypeError as e:
        msg = str(e)
    check("temperature= raised TypeError (not swallowed)", msg is not None)
    check("...the error NAMES the accepted params (guidance, not a bare failure)",
          bool(msg) and "reasoning" in msg and "temperature" in msg)
finally:
    adapters._call_guarded = _orig_guarded
    best_value.select_model_effort = _orig_sel

print(f"\n{'[FAIL]' if _fails else 'OK'} test_call_normalises_caller_kwargs: {len(_fails)} failure(s)")
sys.exit(1 if _fails else 0)
