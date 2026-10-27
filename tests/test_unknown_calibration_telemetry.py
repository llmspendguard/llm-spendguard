"""Missing output telemetry never becomes a zero-sized calibration observation."""
import sqlite3
from unittest.mock import patch
from types import SimpleNamespace

from spendguard import bulkgate, callio


def test_unknown_output_calibration_retains_null():
    with sqlite3.connect(":memory:") as calibration_database, \
            patch.object(bulkgate, "_gate_db", return_value=calibration_database), \
            patch.object(bulkgate, "_warn_truncated"):
        bulkgate.note_response("intent", "model", None)
        unknown_observation = calibration_database.execute(
            "SELECT out_tok,visible_tokens,disposition FROM gate_calls").fetchone()
        assert unknown_observation == (None, None, None)
        assert bulkgate.note_response("intent", "model", None, max_tokens=1, finish_reason="length") is True
        assert calibration_database.execute("SELECT disposition FROM gate_calls ORDER BY rowid DESC").fetchone() == (None,)
        assert bulkgate.is_truncated(None, None, 1) is False
        assert bulkgate.is_truncated("length", None, 1) is True
        assert bulkgate.check_runaway("intent", "model", None) == (False, None)


def test_unknown_corpus_output_and_batch_aggregates():
    with sqlite3.connect(":memory:") as unknown_corpus_database:
        callio._ensure_callio_schema(unknown_corpus_database)
        with patch.object(callio, "_callio_db", return_value=unknown_corpus_database):
            callio.record_io_sample("intent", "provider", "model", "batch", "sample", "prompt", "output", out_tok=None)
            assert unknown_corpus_database.execute("SELECT out_tok FROM call_io").fetchone() == (None,)
    with patch.object(callio, "batch_status", return_value={"batch": {"output_ready": True}}), \
            patch.object(callio, "guarded_collect", return_value=[("sample", "text", {"prompt_tokens": 1})]):
        assert callio.collect_chat_tasks(["batch"], "intent", "model")["usage"]["out_tok"] is None
    unknown_message_client = SimpleNamespace(messages=SimpleNamespace(batches=SimpleNamespace(
        retrieve=lambda batch: SimpleNamespace(processing_status="ended", results_url="available"),
        results=lambda batch: [SimpleNamespace(custom_id="sample", result=SimpleNamespace(
            type="succeeded", message=SimpleNamespace(content=[], usage=SimpleNamespace(input_tokens=1))))])))
    with patch.object(callio, "_anthropic_client", return_value=unknown_message_client):
        assert callio.collect_message_batch(["batch"], "intent", "model")["usage"]["out_tok"] is None


if __name__ == "__main__":
    test_unknown_output_calibration_retains_null()
    test_unknown_corpus_output_and_batch_aggregates()
