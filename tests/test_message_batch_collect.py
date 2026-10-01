"""Offline unit tests for the Anthropic Message Batch COLLECT path (callio.collect_message_batch) — the SETTLE twin
of submit.submit_message_batch. NO network, NO spend: the anthropic client is replaced with a fake that returns
canned retrieve()/results() objects mirroring the REAL SDK shape (grounded via introspection of anthropic 0.111.0:
MessageBatch.processing_status / .results_url; a result's .type ∈ succeeded/errored/canceled/expired; succeeded →
.message with .content blocks + .usage; errored → .error). Sets its OWN fake ANTHROPIC_API_KEY (the suite strips
real keys — else green-local / red-keyless-CI).

Pins:
  · a succeeded text result → results[custom_id] = joined text; usage summed on BOTH axes (in_tok AND out_tok);
  · a succeeded forced-tool/schema result → results[custom_id] = JSON of tool_use.input;
  · an errored result → failed[custom_id], never in results (no silent drop);
  · a result with no custom_id → anomalies (nothing to key it by);
  · a batch not ENDED (or with no results_url) → not_ready [batch_id], its results never pulled;
  · record_io=True captures the REAL in_tok+out_tok into the corpus (the INPUT half fetch_anthropic omits).
"""
import json
import os
import sys
import tempfile

os.environ["SPENDGUARD_HOME"] = os.environ.get("SPENDGUARD_HOME") or tempfile.mkdtemp(prefix="sg-msgcollect-")
os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"
os.environ.setdefault("SPENDGUARD_NO_AUTOINSTALL", "1")
os.environ["ANTHROPIC_API_KEY"] = "sk-ant-test-FAKE"
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

import anthropic                                           # noqa: E402
from spendguard import callio                              # noqa: E402

MODEL = "claude-haiku-4-5"


class _Obj:
    def __init__(self, **kw):
        self.__dict__.update(kw)


class _FakeBatchesCollect:
    def __init__(self, batches, results):
        self._batches = batches          # {batch_id: MessageBatch-like}
        self._results = results          # {batch_id: [IndividualResponse-like]}

    def retrieve(self, bid):
        return self._batches[bid]

    def results(self, bid):
        return iter(self._results.get(bid, []))


class _FakeAnthropicCollect:
    _batches = {}
    _results = {}

    def __init__(self, *a, **kw):
        self.messages = _Obj(batches=_FakeBatchesCollect(_FakeAnthropicCollect._batches,
                                                         _FakeAnthropicCollect._results))


def _usage(in_tok, out_tok, cread=0, ccreate=0):
    return _Obj(input_tokens=in_tok, output_tokens=out_tok,
                cache_read_input_tokens=cread, cache_creation_input_tokens=ccreate)


def _succeeded_text(cid, text, in_tok, out_tok):
    msg = _Obj(content=[_Obj(type="text", text=text)], usage=_usage(in_tok, out_tok))
    return _Obj(custom_id=cid, result=_Obj(type="succeeded", message=msg))


def _succeeded_tool(cid, obj, in_tok, out_tok):
    msg = _Obj(content=[_Obj(type="tool_use", name="result", input=obj)], usage=_usage(in_tok, out_tok))
    return _Obj(custom_id=cid, result=_Obj(type="succeeded", message=msg))


def _errored(cid, err):
    return _Obj(custom_id=cid, result=_Obj(type="errored", error=err))


def _no_custom_id():
    return _Obj(custom_id=None, result=_Obj(type="succeeded", message=_Obj(content=[], usage=_usage(0, 0))))


def main():
    fails = []

    def ck(name, cond, extra=""):
        print(("  [OK] " if cond else "  [FAIL] ") + name + (("  — " + str(extra)) if extra and not cond else ""))
        if not cond:
            fails.append(name)

    ended = _Obj(processing_status="ended", results_url="https://x/results")
    inprog = _Obj(processing_status="in_progress", results_url=None)

    _FakeAnthropicCollect._batches = {"b_ok": ended, "b_wait": inprog}
    _FakeAnthropicCollect._results = {
        "b_ok": [
            _succeeded_text("t1", "hello answer", 120, 30),
            _succeeded_tool("t2", {"label": "spam"}, 200, 15),
            _errored("t3", _Obj(type="overloaded_error", message="server overloaded")),
            _no_custom_id(),
        ],
    }
    anthropic.Anthropic = _FakeAnthropicCollect

    out = callio.collect_message_batch(["b_ok", "b_wait"], "test:collect", MODEL, record_io=False)

    ck("succeeded text → results[custom_id] = joined text", out["results"].get("t1") == "hello answer", out["results"].get("t1"))
    ck("succeeded forced-tool → results[custom_id] = JSON of tool_use.input",
       out["results"].get("t2") == json.dumps({"label": "spam"}), out["results"].get("t2"))
    ck("errored result → failed[custom_id], not in results", "t3" in out["failed"] and "t3" not in out["results"], out["failed"])
    ck("result with no custom_id → anomalies (nothing to key it by)", len(out["anomalies"]) == 1, out["anomalies"])
    ck("collected counts only succeeded rows (2)", out["collected"] == 2, out["collected"])
    ck("not-ended batch → not_ready, results never pulled", out["not_ready"] == ["b_wait"], out["not_ready"])
    ck("usage.in_tok summed across succeeded (120+200)", out["usage"]["in_tok"] == 320, out["usage"]["in_tok"])
    ck("usage.out_tok summed across succeeded (30+15)", out["usage"]["out_tok"] == 45, out["usage"]["out_tok"])

    # require_ready=False still skips a batch with no results_url (nothing to pull)
    out2 = callio.collect_message_batch("b_wait", "test:collect", MODEL, require_ready=False)
    ck("no results_url → not_ready even with require_ready=False", out2["not_ready"] == ["b_wait"], out2["not_ready"])

    # record_io=True captures REAL in_tok+out_tok into the corpus (monkeypatch the module-global recorder)
    captured = {}
    _real = callio.record_io_sample

    def _spy(intent, provider, model, batch_id, cid, prompt, text, in_tok=0, out_tok=0):
        captured[cid] = (in_tok, out_tok)
        return True

    callio.record_io_sample = _spy
    try:
        _FakeAnthropicCollect._results = {"b_ok": [_succeeded_text("r1", "x", 77, 9)]}
        callio.collect_message_batch("b_ok", "test:collect", MODEL, record_io=True)
    finally:
        callio.record_io_sample = _real
    ck("record_io=True captures REAL in_tok+out_tok (both axes)", captured.get("r1") == (77, 9), captured.get("r1"))

    print(f"\n{'[FAIL]' if fails else 'OK'} test_message_batch_collect: {len(fails)} failure(s)")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
