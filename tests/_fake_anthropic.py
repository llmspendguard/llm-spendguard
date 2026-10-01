"""Shared offline fakes for the Anthropic Message Batches SDK shape (anthropic 0.111.0) — ONE place for the fake client
+ the SDK-shaped builders so the batch submit / collect / offload tests don't each grow a divergent copy (the
honestreview cross-file-duplication doctrine). NO network, NO spend. Not a test itself (leading underscore → chunked_suite
globs test_*.py only).

Builders mirror the real shapes grounded by introspection: a MessageBatch has processing_status / results_url /
request_counts / created_at; a result's .type ∈ succeeded/errored/canceled/expired; a succeeded result → .message with
.content blocks + .usage; an errored result → .error.
"""
import datetime
from types import SimpleNamespace


def usage(in_tok=0, out_tok=0, cache_read=0, cache_creation=0):
    return SimpleNamespace(input_tokens=in_tok, output_tokens=out_tok,
                           cache_read_input_tokens=cache_read, cache_creation_input_tokens=cache_creation)


def succeeded_text(custom_id, text, in_tok=0, out_tok=0, cache_read=0):
    msg = SimpleNamespace(content=[SimpleNamespace(type="text", text=text)], usage=usage(in_tok, out_tok, cache_read))
    return SimpleNamespace(custom_id=custom_id, result=SimpleNamespace(type="succeeded", message=msg))


def succeeded_tool(custom_id, obj, in_tok=0, out_tok=0):
    msg = SimpleNamespace(content=[SimpleNamespace(type="tool_use", name="result", input=obj)], usage=usage(in_tok, out_tok))
    return SimpleNamespace(custom_id=custom_id, result=SimpleNamespace(type="succeeded", message=msg))


def errored(custom_id, error):
    return SimpleNamespace(custom_id=custom_id, result=SimpleNamespace(type="errored", error=error))


def result_no_custom_id():
    return SimpleNamespace(custom_id=None,
                           result=SimpleNamespace(type="succeeded", message=SimpleNamespace(content=[], usage=usage())))


def request_counts(n_succeeded):
    return SimpleNamespace(processing=0, succeeded=n_succeeded, errored=0, canceled=0, expired=0)


def batch_obj(bid, status, n, results_url="https://x/results", created=None):
    return SimpleNamespace(id=bid, processing_status=status, results_url=results_url,
                           created_at=created or datetime.datetime.now(datetime.timezone.utc),
                           request_counts=request_counts(n))


class FakeBatches:
    """A fake messages.batches covering every method the batch paths call: retrieve(bid), results(bid), list(limit=),
    and create(requests=) (records each call's requests; returns `create_result` or raises `create_raises`)."""

    def __init__(self, *, batches=None, results=None, list_return=None, create_result=None, create_raises=None):
        self.batches = dict(batches or {})          # bid -> batch_obj (retrieve)
        self.results_map = dict(results or {})      # bid -> [result rows]
        self.list_return = list(list_return or [])  # [batch_obj] newest-first (the orphan scan)
        self.create_result = create_result          # a batch_obj to return from create (else a default)
        self.create_raises = create_raises          # an exception to raise from create (else None)
        self.create_calls = []                      # recorded [requests] per create() call

    def retrieve(self, bid):
        return self.batches[bid]

    def results(self, bid):
        return iter(self.results_map.get(bid, []))

    def list(self, limit=100):
        return iter(self.list_return)

    def create(self, requests=None, **kw):
        self.create_calls.append(list(requests or []))
        if self.create_raises is not None:
            raise self.create_raises
        return self.create_result or batch_obj("msgbatch_fake", "in_progress", len(list(requests or [])))


class FakeAnthropic:
    """A fake anthropic.Anthropic whose .messages.batches is the given FakeBatches. Construct with the FakeBatches the
    test configured; accepts (and ignores) api_key/kwargs so it drops in for `anthropic.Anthropic(api_key=…)`."""

    def __init__(self, fake_batches=None, *a, **kw):
        self.messages = SimpleNamespace(batches=fake_batches if fake_batches is not None else FakeBatches())
