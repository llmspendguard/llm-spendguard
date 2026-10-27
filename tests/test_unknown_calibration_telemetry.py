"""Missing output telemetry never becomes a zero-sized calibration observation."""
from unittest.mock import patch
from types import SimpleNamespace

from _pytest_fixture_bridge import align_test_startup_home

align_test_startup_home(prefix=__name__)

from spendguard import bulkgate, callio


def test_unknown_output_calibration_retains_null():
    with patch.object(bulkgate, "_warn_truncated"):
        bulkgate.note_response("intent", "model", None)
        with bulkgate.config.ledger_op("bulkgate", bulkgate._ensure_gate_schema) as calibration_database:
            unknown_observation = calibration_database.execute(
                "SELECT out_tok,visible_tokens,disposition FROM gate_calls").fetchone()
            assert tuple(unknown_observation) == (None, None, None)
        assert bulkgate.note_response("intent", "model", None, max_tokens=1, finish_reason="length") is True
        with bulkgate.config.ledger_op("bulkgate", bulkgate._ensure_gate_schema) as calibration_database:
            assert tuple(calibration_database.execute(
                "SELECT disposition FROM gate_calls ORDER BY rowid DESC").fetchone()) == (None,)
        assert bulkgate.is_truncated(None, None, 1) is False
        assert bulkgate.is_truncated("length", None, 1) is True
        assert bulkgate.check_runaway("intent", "model", None) == (False, None)


def test_unknown_corpus_output_and_batch_aggregates():
    callio.record_io_sample("intent", "provider", "model", "batch", "sample", "prompt", "output", out_tok=None)
    with callio.config.ledger_op("callio", callio._ensure_callio_schema) as unknown_corpus_database:
        assert tuple(unknown_corpus_database.execute("SELECT out_tok FROM call_io").fetchone()) == (None,)
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
