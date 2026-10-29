"""Optional ledger bookkeeping catches writer startup and admission failures."""
import sqlite3
import tempfile
from pathlib import Path
from unittest.mock import patch

from spendguard import (batch_tracker, best_value, bulkgate, calibrate, callio, config, conv, gate, guard,
                        intent_model_prior, lane_bandit, ledger_writer, reliability, semcache)
from spendguard.ledger_read import LedgerQueryDeadline

OPTIONAL_WRITER_TEST_LEDGER = "malformed-bookkeeping.sqlite"
OPTIONAL_WRITER_TEST_PAYLOAD = "This is not a SQLite database"
OPTIONAL_WRITER_TEST_LABEL = "optional-writer-contract"
OPTIONAL_WRITER_TEST_AMOUNT = 1.0
OPTIONAL_WRITER_TEST_DEADLINE_S = 0.01


def exercise_optional_writer_dispatch_contract():
    optional_operations = (
        (conv._persist_user_ask_verdicts, lambda: conv._persist_user_ask_verdicts(
            [], {}, {}, OPTIONAL_WRITER_TEST_LABEL)),
        (conv._evidence_put, lambda: conv._evidence_put(
            OPTIONAL_WRITER_TEST_LABEL, {}, OPTIONAL_WRITER_TEST_LABEL)),
        (best_value._store_bestvalue_verdict, lambda: best_value._store_bestvalue_verdict(
            OPTIONAL_WRITER_TEST_LABEL, None, OPTIONAL_WRITER_TEST_LABEL, {})),
        (intent_model_prior._store_intent_prior, lambda: intent_model_prior._store_intent_prior(
            OPTIONAL_WRITER_TEST_LABEL, OPTIONAL_WRITER_TEST_LABEL, [])),
        (batch_tracker.register_batch, lambda: batch_tracker.register_batch(
            OPTIONAL_WRITER_TEST_LABEL, OPTIONAL_WRITER_TEST_LABEL, OPTIONAL_WRITER_TEST_LABEL,
            OPTIONAL_WRITER_TEST_LABEL, OPTIONAL_WRITER_TEST_AMOUNT)),
        (batch_tracker._pending_confirm, lambda: batch_tracker._pending_confirm(
            OPTIONAL_WRITER_TEST_LABEL, OPTIONAL_WRITER_TEST_LABEL)),
        (batch_tracker._pending_clear, lambda: batch_tracker._pending_clear(OPTIONAL_WRITER_TEST_LABEL)),
        (batch_tracker._set_status, lambda: batch_tracker._set_status(
            OPTIONAL_WRITER_TEST_LABEL, OPTIONAL_WRITER_TEST_LABEL)),
        (semcache._persist_semcache_entry, lambda: semcache._persist_semcache_entry(
            OPTIONAL_WRITER_TEST_LABEL, OPTIONAL_WRITER_TEST_LABEL, OPTIONAL_WRITER_TEST_LABEL, None)),
        (reliability._persist_lane_remediation_verdict, lambda: reliability._persist_lane_remediation_verdict(
            OPTIONAL_WRITER_TEST_LABEL, OPTIONAL_WRITER_TEST_LABEL, OPTIONAL_WRITER_TEST_LABEL, {})),
        (reliability._prune_superseded_lane_health, lambda: reliability._prune_superseded_lane_health([], [], [])),
        (reliability._persist_health, lambda: reliability._persist_health({}, None)),
        (guard.record_saving, lambda: guard.record_saving(OPTIONAL_WRITER_TEST_LABEL, OPTIONAL_WRITER_TEST_AMOUNT)),
        (guard.record_decision, lambda: guard.record_decision(
            OPTIONAL_WRITER_TEST_LABEL, OPTIONAL_WRITER_TEST_LABEL, OPTIONAL_WRITER_TEST_LABEL,
            OPTIONAL_WRITER_TEST_AMOUNT, OPTIONAL_WRITER_TEST_AMOUNT, OPTIONAL_WRITER_TEST_AMOUNT)),
        (calibrate.record_prediction, lambda: calibrate.record_prediction(
            OPTIONAL_WRITER_TEST_LABEL, OPTIONAL_WRITER_TEST_LABEL, OPTIONAL_WRITER_TEST_LABEL,
            OPTIONAL_WRITER_TEST_AMOUNT)),
        (lane_bandit.record_trial, lambda: lane_bandit.record_trial(
            OPTIONAL_WRITER_TEST_LABEL, OPTIONAL_WRITER_TEST_LABEL, OPTIONAL_WRITER_TEST_LABEL, True)),
        (callio.set_quality, lambda: callio.set_quality(OPTIONAL_WRITER_TEST_LABEL, True)),
        (reliability.note_lane_down, lambda: reliability.note_lane_down(OPTIONAL_WRITER_TEST_LABEL, OPTIONAL_WRITER_TEST_LABEL)),
        (reliability.note_lane_ok, lambda: reliability.note_lane_ok(OPTIONAL_WRITER_TEST_LABEL)),
        (reliability.note_lane_auth_down, lambda: reliability.note_lane_auth_down(OPTIONAL_WRITER_TEST_LABEL)),
        (reliability.note_lane_model_shift, lambda: reliability.note_lane_model_shift(
            OPTIONAL_WRITER_TEST_LABEL, OPTIONAL_WRITER_TEST_LABEL, OPTIONAL_WRITER_TEST_LABEL)),
        (reliability.note_lane_model_ok, lambda: reliability.note_lane_model_ok(
            OPTIONAL_WRITER_TEST_LABEL, OPTIONAL_WRITER_TEST_LABEL)),
        (bulkgate.note_latency, lambda: bulkgate.note_latency(
            OPTIONAL_WRITER_TEST_LABEL, OPTIONAL_WRITER_TEST_LABEL, OPTIONAL_WRITER_TEST_AMOUNT)),
        (bulkgate.note_response, lambda: bulkgate.note_response(
            OPTIONAL_WRITER_TEST_LABEL, OPTIONAL_WRITER_TEST_LABEL, OPTIONAL_WRITER_TEST_AMOUNT)),
    )
    with tempfile.TemporaryDirectory() as directory:
        malformed = Path(directory) / OPTIONAL_WRITER_TEST_LEDGER
        malformed.write_text(OPTIONAL_WRITER_TEST_PAYLOAD)
        with patch.object(config, "db_path", return_value=malformed):
            for recorder, operation in optional_operations:
                with patch.object(config, "warn_once") as warning:
                    operation()
                    assert warning.called, f"{recorder.__name__} must warn on malformed ledger"
            try:
                bulkgate.record_estimate(OPTIONAL_WRITER_TEST_LABEL, OPTIONAL_WRITER_TEST_LABEL,
                                        OPTIONAL_WRITER_TEST_AMOUNT, OPTIONAL_WRITER_TEST_AMOUNT)
            except sqlite3.DatabaseError:
                pass
            else:
                raise AssertionError("required writer must propagate malformed-ledger failure")
    for recorder, operation in optional_operations:
        for ordinary_error in (OSError(OPTIONAL_WRITER_TEST_LABEL),
                               LedgerQueryDeadline(OPTIONAL_WRITER_TEST_DEADLINE_S)):
            with patch.object(ledger_writer, "run_ledger_write", side_effect=ordinary_error), \
                    patch.object(config, "warn_once") as warning:
                operation()
                assert warning.called, f"{recorder.__name__} must warn on dispatch failure"
        refusal = gate.deliberate_stop_types()[0](OPTIONAL_WRITER_TEST_LABEL)
        with patch.object(ledger_writer, "run_ledger_write", side_effect=refusal):
            try:
                operation()
            except type(refusal):
                pass
            else:
                raise AssertionError(f"{recorder.__name__} swallowed deliberate refusal")
        if recorder.__module__ in {guard.__name__, bulkgate.__name__, calibrate.__name__,
                                   callio.__name__, lane_bandit.__name__, reliability.__name__} and recorder not in {
                                       reliability._prune_superseded_lane_health,
                                       reliability._persist_lane_remediation_verdict}:
            with patch.object(ledger_writer, "run_ledger_write", side_effect=lambda callback: callback()), \
                    patch.object(config, "pooled_ledger_conn", side_effect=refusal), \
                    patch.object(config, "fresh_ledger_conn", side_effect=refusal), \
                    patch.object(reliability, "_health_db", side_effect=refusal), \
                    patch.object(config, "ledger_op", side_effect=refusal):
                try:
                    operation()
                except type(refusal):
                    pass
                else:
                    raise AssertionError(f"{recorder.__name__} swallowed callback deliberate refusal")
        with patch.object(ledger_writer, "run_ledger_write",
                          side_effect=LedgerQueryDeadline(OPTIONAL_WRITER_TEST_DEADLINE_S)), \
                patch.object(config, "warn_once") as warning:
            result = operation()
            assert warning.called, f"{recorder.__name__} must warn on dispatch deadline"
            if recorder is calibrate.record_prediction:
                assert result is False
    with patch.object(ledger_writer, "run_ledger_write",
                      side_effect=LedgerQueryDeadline(OPTIONAL_WRITER_TEST_DEADLINE_S)):
        try:
            bulkgate.record_estimate(OPTIONAL_WRITER_TEST_LABEL, OPTIONAL_WRITER_TEST_LABEL,
                                    OPTIONAL_WRITER_TEST_AMOUNT, OPTIONAL_WRITER_TEST_AMOUNT)
        except LedgerQueryDeadline:
            pass
        else:
            raise AssertionError("required writer must propagate admission deadline failure")
        with patch.object(config, "warn_once", side_effect=RuntimeError(OPTIONAL_WRITER_TEST_LABEL)):
            for _, operation in optional_operations:
                operation()
    print("PASS: optional writer startup and deadline failures warn and return; required writes fail closed")


if __name__ == "__main__":
    exercise_optional_writer_dispatch_contract()
