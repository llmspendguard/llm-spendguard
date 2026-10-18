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

os.environ.setdefault("ANTHROPIC_API_KEY", "sk-ant-test-offline")

import anthropic                                                    # noqa: E402
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


if __name__ == "__main__":
    import os as _os, sys as _sys
    _sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
    from _pytest_fixture_bridge import run_pytest_style
    _sys.exit(run_pytest_style(globals()))
