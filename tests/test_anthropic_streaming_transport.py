"""The anthropic transport must STREAM — a non-streaming request is vetoed by the SDK at large max_tokens.

WHY THIS GUARD EXISTS. `max_tokens` is a TERMINATION bound sized from measured need, so a correctly-sized cap
on a slow model is large. The Anthropic SDK refuses a non-streaming request whose max_tokens implies a run
over ten minutes, so the correct cap and the non-streaming transport are mutually exclusive: every large-cap
call died with `transport_error` while the call itself was fine. Reverting to `messages.create` would bring
that back, and it would look like a provider outage rather than a client bug — which is exactly how it
presented the first time.

HOW THIS FILE ASSERTS IT — AND WHY IT CHANGED. The checks here used to read `inspect.getsource(_call_once)`
and look for the substrings `messages.stream(` / `get_final_message(`. That is a mechanical proxy standing in
for a judgement about what the code DOES: it fell silent the moment `_call_once` grew (the naive
`.split("else:")[0]` slice stopped reaching the real stream call, so the guard failed on correct code), and
it would pass on a comment or a dead branch. Each check below now drives a real anthropic call against a
RECORDING transport and asserts on what reached the wire: that `messages.stream()` was used, `messages.create()`
never was, and the answer was resolved from the stream's final message. No source text is inspected.

Offline: a fake Anthropic client, test_runner's dead proxy over any real call, and a fake key (test_runner
strips real provider keys, and the metered path short-circuits at the no-key check BEFORE the faked transport
is reached, so the script must supply its own — the green-locally/red-on-keyless-CI trap).
"""
import os
from pathlib import Path

os.environ.setdefault("ANTHROPIC_API_KEY", "sk-ant-test-offline")

import anthropic                                                    # noqa: E402
import httpx                                                       # noqa: E402
from spendguard import adapters                                     # noqa: E402

MODEL = "anthropic:claude-opus-4-8"
CAPTION = "a faithful one-sentence answer"


def _recording_anthropic():
    """A fake anthropic.Anthropic that records whether stream()/create()/get_final_message() were used and
    returns a well-formed final message, so a real adapters call completes without touching the network."""
    box = {"stream_called": False, "create_called": False, "final_message_read": False, "stream_kw": None}

    class _Usage:
        input_tokens = 10
        output_tokens = 5
        cache_creation_input_tokens = 0
        cache_read_input_tokens = 0

    class _Block:
        type = "text"
        text = CAPTION

    class _Message:
        content = [_Block()]
        usage = _Usage()
        stop_reason = "end_turn"
        model = "claude-opus-4-8"

    class _Stream:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get_final_message(self):
            box["final_message_read"] = True
            return _Message()

    class _Messages:
        def stream(self, **kw):
            box["stream_called"] = True
            box["stream_kw"] = kw
            return _Stream()

        def create(self, **kw):          # must NEVER be reached — the SDK vetoes it above a large max_tokens
            box["create_called"] = True
            raise AssertionError("messages.create() was called — the anthropic branch must stream")

    class _Anthropic:
        def __init__(self, *a, **kw):
            self.messages = _Messages()

        def close(self):
            pass

    return _Anthropic, box


def test_anthropic_path_streams_and_never_calls_create(monkeypatch):
    fake, box = _recording_anthropic()
    monkeypatch.setattr(anthropic, "Anthropic", fake)
    r = adapters._call_once(MODEL, "Say the caption.", max_tokens=64, _skip_lane=True)
    assert not r.get("error"), r.get("error")
    assert box["stream_called"], "the anthropic branch must send via messages.stream()"
    assert not box["create_called"], (
        "messages.create() reached the wire — it fails with 'Streaming is required for operations that may "
        "take longer than 10 minutes' on any correctly-sized cap")
    assert r.get("text") == CAPTION


def test_final_message_is_read_from_the_stream_not_the_stream_object(monkeypatch):
    # A stream yields events; usage and content live on the FINAL message. Reading the context manager itself
    # would give an object with no .usage, and the token counts would silently become zero — the same
    # absence-as-zero failure the ledger markers exist to prevent.
    fake, box = _recording_anthropic()
    monkeypatch.setattr(anthropic, "Anthropic", fake)
    r = adapters._call_once(MODEL, "Say the caption.", max_tokens=64, _skip_lane=True)
    assert box["final_message_read"], "must resolve the stream to its final message before reading usage/content"
    assert r.get("text") == CAPTION             # the caption lives on the final message, not the stream object
    assert not r.get("error"), r.get("error")


def test_sdk_rejected_sampling_kwarg_is_dropped_before_stream(monkeypatch):
    # Proof of bite against the installed SDK contract: argument binding fails before the method body can open a
    # transport, so this is deterministic and offline.  This was the exact production failure under SDK 1.15.0.
    rejected_kwargs = {"model": "probe-model", "max_tokens": 1,
                       "messages": [{"role": "user", "content": "ok"}], "temperature": 0}
    try:
        anthropic.resources.messages.Messages.stream(object(), **rejected_kwargs)
    except TypeError as exc:
        assert "temperature" in str(exc)
    else:
        raise AssertionError("SDK stream unexpectedly accepted the regression-triggering temperature kwarg")

    fake, box = _recording_anthropic()
    monkeypatch.setattr(anthropic, "Anthropic", fake)
    result = adapters._call_once(MODEL, "Say the caption.", max_tokens=64, temperature=0,
                                 _skip_lane=True)
    assert not result.get("error"), result.get("error")
    assert "temperature" not in box["stream_kw"]
    assert result["gen_params_dropped"] == {"temperature": 0}
    assert result["gen_params_applied"] is None


def test_uninspectable_sdk_contract_records_why_knobs_were_dropped(monkeypatch):
    import inspect
    fake, box = _recording_anthropic()
    monkeypatch.setattr(anthropic, "Anthropic", fake)
    monkeypatch.setattr(inspect, "signature", lambda _callable: (_ for _ in ()).throw(ValueError("uninspectable")))
    result = adapters._call_once(MODEL, "Say the caption.", max_tokens=64, temperature=0, _skip_lane=True)
    assert not result.get("error"), result.get("error")
    assert "temperature" not in box["stream_kw"]
    assert result["gen_params_dropped"] == {"temperature": 0}
    assert result["gen_params_dropped_reason"] == adapters.SDK_CONTRACT_UNAVAILABLE_REASON


def _exception_raised_from_file(filename, exception):
    namespace = {"exception": exception}
    try:
        exec(compile("raise exception", str(filename), "exec"), namespace)
    except BaseException as raised:
        return raised
    raise AssertionError("constructed exception did not raise")


def test_structural_failure_kinds_use_transport_and_status_range_only():
    provider_origin = Path(anthropic.__file__).resolve().parent / "origin_contract_test.py"
    spendguard_origin = Path(adapters.__file__).resolve().parent / "origin_contract_test.py"
    unknown_origin = Path(__file__).resolve().parent / "origin_contract_test.py"
    cases = (
        (_exception_raised_from_file(provider_origin, TypeError("invalid client argument")), None, "indeterminate"),
        (_exception_raised_from_file(spendguard_origin, ValueError("malformed provider response")), None,
         "indeterminate"),
        (_exception_raised_from_file(unknown_origin, TypeError("origin is not owned")), None, "indeterminate"),
        (RuntimeError("served server failure"), 503, "provider_error"),
        (RuntimeError("provider throttled request"), 429, "provider_error"),
        (RuntimeError("served client rejection"), 400, "client_bug"),
        (RuntimeError("served missing resource"), 404, "client_bug"),
        # HTTP 408 means the client did not complete its request within the server's wait window.
        (RuntimeError("client request timed out"), 408, "client_bug"),
        (RuntimeError("served non-error status"), 418, "client_bug"),
        (RuntimeError("served unexpected status"), 399, "indeterminate"),
        (ConnectionError("transport failed"), None, "provider_unreachable"),
        (httpx.ConnectError("connection refused"), None, "provider_unreachable"),
    )
    for exception, status_code, expected_kind in cases:
        assert adapters._exception_failure_kind(exception, status_code) == expected_kind


def test_internal_redirect_pin_is_applied_at_guarded_lane_selection_boundary(monkeypatch):
    from spendguard import plan_admission
    redirected_model = "openai:redirected-model"
    redirected_lane = adapters._LANES[adapters.provider_for(redirected_model)][0]
    observed = {}

    monkeypatch.setattr(plan_admission, "decide", lambda *args, **kwargs: {
        "action": "redirect", "model": redirected_model, "requested_model": MODEL, "lane": redirected_lane,
        "reason": "offline pin regression guard"
    })
    monkeypatch.setattr(adapters, "_route_through_queue_enabled", lambda: False)

    # _internal_pin is required here with no default: deleting it from call→_call_guarded forwarding makes this
    # controlled boundary double raise TypeError (the historical failure), while dropping confinement flips _no_sub.
    def _guarded_boundary(model, prompt, max_tokens=None, sig=None, retries=None, *, _internal_pin, **kwargs):
        observed.update(model=model, internal_pin=_internal_pin, no_sub=kwargs.get("_no_sub"))
        return {"text": "ok", "error": None, "model": model, "executor": "test"}

    monkeypatch.setattr(adapters, "_call_guarded", _guarded_boundary)
    result = adapters.call(MODEL, "pin this route", max_tokens=64, intent="test:internal-pin-boundary")
    assert not result.get("error"), result
    assert observed == {"model": redirected_model, "internal_pin": True, "no_sub": True}


if __name__ == "__main__":
    import os as _os, sys as _sys
    _sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
    from _pytest_fixture_bridge import run_pytest_style
    _sys.exit(run_pytest_style(globals()))
