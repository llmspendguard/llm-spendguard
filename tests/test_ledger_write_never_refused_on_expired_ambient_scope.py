"""Durable writes budget ledger work independently of ambient request age."""
import json
import pathlib
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import patch

from _pytest_fixture_bridge import align_test_startup_home

align_test_startup_home(prefix=__name__)

from spendguard import calls, config, ledger_deadletter, ledger_read, ledger_writer
from spendguard.ledger import SpendLedger

WRITE_AMBIENT_EXPIRED_OFFSET_S = 1.0
WRITE_AMBIENT_EVENT_USD = "1.125000001"
WRITE_AMBIENT_PROVIDER = "test-write-ambient-provider"
WRITE_AMBIENT_MODEL = "test-write-ambient-model"
WRITE_AMBIENT_EVENT = {"kind": "realtime", "usd": WRITE_AMBIENT_EVENT_USD,
                       "source": __name__, "dedup_key": __name__}
WRITE_AMBIENT_COUNT_SQL = "SELECT COUNT(*) FROM spend_events WHERE source=?"
WRITE_AMBIENT_CALL_COUNT_SQL = "SELECT COUNT(*) FROM calls WHERE intent=?"


class LedgerWriteExpiredAmbientScopeGuards(unittest.TestCase):
    def setUp(self):
        self.write_home_owner = tempfile.TemporaryDirectory(dir=config.HOME)
        self.addCleanup(self.write_home_owner.cleanup)
        self.write_path = pathlib.Path(self.write_home_owner.name) / pathlib.Path(__file__).stem
        self.write_path_patch = patch.object(config, "db_path", return_value=self.write_path)
        self.write_path_patch.start()
        self.addCleanup(self.write_path_patch.stop)
        self.addCleanup(ledger_writer.close_ledger_writers)
        self.write_spool = self.write_path.with_suffix(ledger_writer.LEDGER_WRITER_LOCK_SUFFIX + ".jsonl")
        # Create schema outside the request, exactly as an already-running daemon does.
        self.write_schema_connection = sqlite3.connect(self.write_path, check_same_thread=False)
        self.write_ledger = SpendLedger(self.write_path, owned_connection=self.write_schema_connection)
        self.addCleanup(self.write_schema_connection.close)

    def expire_write_ambient_scope(self, scope):
        scope.deadline_at = time.monotonic() - WRITE_AMBIENT_EXPIRED_OFFSET_S

    def invoke_delegate_call_accounting_write(self):
        return calls.record_call(WRITE_AMBIENT_PROVIDER, WRITE_AMBIENT_MODEL, "realtime",
                                 WRITE_AMBIENT_EVENT_USD, intent=__name__, project=__name__)

    def stage_write_ambient_deadletter(self):
        ledger_deadletter.append_spend_deadletter_event(self.write_spool, {"event": WRITE_AMBIENT_EVENT})
        return self.write_spool.read_bytes()

    def count_write_ambient_committed_rows(self, statement):
        with sqlite3.connect(self.write_path) as connection:
            return connection.execute(statement, (__name__,)).fetchone()[0]

    def test_delegate_accounting_completes_after_ambient_expiry(self):
        with ledger_read.ledger_request_scope() as ambient:
            self.expire_write_ambient_scope(ambient)
            self.assertTrue(self.invoke_delegate_call_accounting_write())
        self.assertEqual(self.count_write_ambient_committed_rows(WRITE_AMBIENT_CALL_COUNT_SQL), 1)

    def test_deadletter_replay_completes_after_ambient_expiry(self):
        self.stage_write_ambient_deadletter()
        with ledger_read.ledger_request_scope() as ambient:
            self.expire_write_ambient_scope(ambient)
            replay = ledger_deadletter.drain_spend_deadletter_events((self.write_spool,))
        self.assertEqual(replay, {"committed": 1, "retained": 0, "errors": []})
        self.assertEqual(self.count_write_ambient_committed_rows(WRITE_AMBIENT_COUNT_SQL), 1)
        self.assertEqual(self.write_spool.read_bytes(), b"")

    def test_cancelled_delegate_accounting_still_aborts(self):
        with ledger_read.ledger_request_scope() as ambient:
            ambient.cancel_ledger_request()
            with self.assertRaises(ledger_read.LedgerRequestAbandoned):
                self.invoke_delegate_call_accounting_write()

    def test_cancelled_deadletter_replay_preserves_full_evidence(self):
        evidence = self.stage_write_ambient_deadletter()
        with ledger_read.ledger_request_scope() as ambient:
            ambient.cancel_ledger_request()
            with self.assertRaises(ledger_read.LedgerRequestAbandoned):
                ledger_deadletter.drain_spend_deadletter_events((self.write_spool,))
        self.assertEqual(self.write_spool.read_bytes(), evidence)
        self.assertEqual(self.count_write_ambient_committed_rows(WRITE_AMBIENT_COUNT_SQL), 0)

    def test_active_write_budget_expiry_rolls_back_and_retains_retry(self):
        evidence = self.stage_write_ambient_deadletter()
        original_record_event = SpendLedger.record_event

        def expire_after_recording_write_evidence(ledger, event, **kwargs):
            value = original_record_event(ledger, event, **kwargs)
            self.expire_write_ambient_scope(ledger_read.current_ledger_request_scope())
            return value

        with ledger_read.ledger_request_scope(), patch.object(
                SpendLedger, "record_event", expire_after_recording_write_evidence):
            with self.assertRaises(ledger_read.LedgerQueryDeadline):
                ledger_deadletter.drain_spend_deadletter_events((self.write_spool,))
        self.assertEqual(self.write_spool.read_bytes(), evidence)
        self.assertEqual(json.loads(evidence)["event"], WRITE_AMBIENT_EVENT)
        self.assertEqual(self.count_write_ambient_committed_rows(WRITE_AMBIENT_COUNT_SQL), 0)

    def test_transport_cancel_during_write_rolls_back_and_retains_retry(self):
        evidence = self.stage_write_ambient_deadletter()
        original_record_event = SpendLedger.record_event
        with ledger_read.ledger_request_scope() as ambient:
            def cancel_after_recording_write_evidence(ledger, event, **kwargs):
                value = original_record_event(ledger, event, **kwargs)
                ambient.cancel_ledger_request()
                return value

            with patch.object(SpendLedger, "record_event", cancel_after_recording_write_evidence):
                with self.assertRaises(ledger_read.LedgerRequestAbandoned):
                    ledger_deadletter.drain_spend_deadletter_events((self.write_spool,))
        self.assertEqual(json.loads(self.write_spool.read_bytes())["event"], json.loads(evidence)["event"])
        self.assertEqual(self.count_write_ambient_committed_rows(WRITE_AMBIENT_COUNT_SQL), 0)

    def test_nested_write_cannot_renew_expired_active_operation(self):
        nested_write_invocations = []

        def attempt_nested_write_after_operation_expiry():
            SpendLedger(self.write_path).record_event(WRITE_AMBIENT_EVENT)
            self.expire_write_ambient_scope(ledger_read.current_ledger_request_scope())
            ledger_writer.run_ledger_write(lambda: nested_write_invocations.append(__name__))

        with ledger_read.ledger_request_scope():
            with self.assertRaises(ledger_read.LedgerQueryDeadline):
                ledger_writer.run_ledger_write(attempt_nested_write_after_operation_expiry)
        self.assertEqual(nested_write_invocations, [])
        self.assertEqual(self.count_write_ambient_committed_rows(WRITE_AMBIENT_COUNT_SQL), 0)


if __name__ == "__main__":
    unittest.main()
