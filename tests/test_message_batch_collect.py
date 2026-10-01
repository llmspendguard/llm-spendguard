"""Offline unit tests for the Anthropic Message Batch COLLECT path (callio.collect_message_batch) — the SETTLE twin
of submit.submit_message_batch. NO network, NO spend: the anthropic client is the shared _fake_anthropic fake, returning
canned retrieve()/results() objects mirroring the REAL SDK shape. Sets its OWN fake ANTHROPIC_API_KEY (the suite strips
real keys — else green-local / red-keyless-CI).

Pins:
  · a succeeded text result → results[custom_id] = joined text; usage summed on BOTH axes (in_tok AND out_tok);
  · a succeeded forced-tool/schema result → results[custom_id] = JSON of tool_use.input;
  · an errored result → failed[custom_id], never in results (no silent drop);
  · a result with no custom_id → anomalies (nothing to key it by);
  · a batch not ENDED (or with no results_url) → not_ready [batch_id], its results never pulled;
  · in_tok is the TOTAL read-side input = fresh + cache_read (the input half fetch_anthropic omits), summed + per-row to the corpus.
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

from _fake_anthropic import (FakeAnthropic, FakeBatches, batch_obj, succeeded_text,   # noqa: E402
                             succeeded_tool, errored, result_no_custom_id)
from spendguard import callio                                                          # noqa: E402

MODEL = "claude-haiku-4-5"


def main():
    fails = []

    def ck(name, cond, extra=""):
        print(("  [OK] " if cond else "  [FAIL] ") + name + (("  — " + str(extra)) if extra and not cond else ""))
        if not cond:
            fails.append(name)

    fb = FakeBatches(
        batches={"b_ok": batch_obj("b_ok", "ended", 2),
                 "b_wait": batch_obj("b_wait", "in_progress", 0, results_url=None)},
        results={"b_ok": [
            succeeded_text("t1", "hello answer", 120, 30, cache_read=50),   # fresh 120 + cache_read 50 → total in_tok 170
            succeeded_tool("t2", {"label": "spam"}, 200, 15),
            errored("t3", "server overloaded"),
            result_no_custom_id(),
        ]})
    _orig = callio._anthropic_client
    callio._anthropic_client = lambda: FakeAnthropic(fb)
    try:
        out = callio.collect_message_batch(["b_ok", "b_wait"], "test:collect", MODEL, record_io=False)

        ck("succeeded text → results[custom_id] = joined text", out["results"].get("t1") == "hello answer", out["results"].get("t1"))
        ck("succeeded forced-tool → results[custom_id] = JSON of tool_use.input",
           out["results"].get("t2") == json.dumps({"label": "spam"}), out["results"].get("t2"))
        ck("errored result → failed[custom_id], not in results", "t3" in out["failed"] and "t3" not in out["results"], out["failed"])
        ck("result with no custom_id → anomalies (nothing to key it by)", len(out["anomalies"]) == 1, out["anomalies"])
        ck("collected counts only succeeded rows (2)", out["collected"] == 2, out["collected"])
        ck("not-ended batch → not_ready, results never pulled", out["not_ready"] == ["b_wait"], out["not_ready"])
        ck("usage.in_tok = fresh + cache_read summed ((120+50)+200=370)", out["usage"]["in_tok"] == 370, out["usage"]["in_tok"])
        ck("usage.cache_read summed separately (50)", out["usage"]["cache_read"] == 50, out["usage"]["cache_read"])
        ck("usage.out_tok summed across succeeded (30+15)", out["usage"]["out_tok"] == 45, out["usage"]["out_tok"])

        out2 = callio.collect_message_batch("b_wait", "test:collect", MODEL, require_ready=False)
        ck("no results_url → not_ready even with require_ready=False", out2["not_ready"] == ["b_wait"], out2["not_ready"])

        # record_io=True captures REAL in_tok+out_tok into the corpus (monkeypatch the module-global recorder)
        captured = {}
        _real_rec = callio.record_io_sample

        def _spy(intent, provider, model, batch_id, cid, prompt, text, in_tok=0, out_tok=0):
            captured[cid] = (in_tok, out_tok)
            return True

        callio.record_io_sample = _spy
        try:
            fb.results_map = {"b_ok": [succeeded_text("r1", "x", 77, 9, cache_read=8)]}   # fresh 77 + cache_read 8 → 85
            callio.collect_message_batch("b_ok", "test:collect", MODEL, record_io=True)
        finally:
            callio.record_io_sample = _real_rec
        ck("record_io=True captures REAL in_tok(total)+out_tok (both axes)", captured.get("r1") == (85, 9), captured.get("r1"))
    finally:
        callio._anthropic_client = _orig

    print(f"\n{'[FAIL]' if fails else 'OK'} test_message_batch_collect: {len(fails)} failure(s)")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
