"""The anthropic transport must STREAM — a non-streaming request is vetoed by the SDK at large max_tokens.

WHY THIS GUARD EXISTS. `max_tokens` is a TERMINATION bound sized from measured need, so a correctly-sized cap
on a slow model is large. The Anthropic SDK refuses a non-streaming request whose max_tokens implies a run
over ten minutes, so the correct cap and the non-streaming transport are mutually exclusive: every large-cap
call died with `transport_error` while the call itself was fine. Reverting to `messages.create` would bring
that back, and it would look like a provider outage rather than a client bug — which is exactly how it
presented the first time.
"""
import inspect

from spendguard import adapters


def test_anthropic_path_streams_and_never_calls_create():
    src = inspect.getsource(adapters.call)
    anth = src.split('if spec["kind"] == "anthropic":')[1].split("else:")[0]
    assert "messages.stream(" in anth, (
        "the anthropic branch must use messages.stream(); non-streaming is vetoed by the SDK above a "
        "max_tokens threshold the SDK owns, and a correctly-sized termination bound exceeds it")
    assert "messages.create(" not in anth, (
        "messages.create() is back in the anthropic branch — it fails with 'Streaming is required for "
        "operations that may take longer than 10 minutes' on any correctly-sized cap")


def test_final_message_is_read_from_the_stream_not_the_stream_object():
    """A stream yields events; usage and content live on the FINAL message. Reading the context manager
    itself would give an object with no .usage, and the token counts would silently become zero — the same
    absence-as-zero failure the ledger markers exist to prevent."""
    src = inspect.getsource(adapters.call)
    anth = src.split('if spec["kind"] == "anthropic":')[1].split("else:")[0]
    assert "get_final_message()" in anth, "must resolve the stream to its final message before reading usage"
