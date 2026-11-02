"""All ambient entry paths retain fresh operation budgets and cancellation."""
import datetime
import fcntl
import json
import pathlib
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import patch

from _pytest_fixture_bridge import align_test_startup_home

align_test_startup_home(prefix=__name__)

from spendguard import budget, config, content_retention, ledger_deadletter, ledger_read, ledger_writer
from spendguard.content_compression import decompress_retained_text
from spendguard.ledger import SpendLedger
from spendguard.mcp_fd_lifecycle import _run_mcp_deadletter_drain

AMBIENT_GUARD_EXPIRED_OFFSET_S = 1.0
AMBIENT_GUARD_AMOUNT = "1.125000001"
AMBIENT_GUARD_NEGATIVE_BUDGET_S = 0.02
AMBIENT_GUARD_RECURSIVE_STEPS = 1000000000
AMBIENT_GUARD_SLOW_SQL = """WITH RECURSIVE work(value) AS
(VALUES(0) UNION ALL SELECT value+1 FROM work WHERE value<?) SELECT SUM(value) FROM work"""
AMBIENT_GUARD_COUNT_SQL = "SELECT COUNT(*) FROM spend_events WHERE source=?"
AMBIENT_GUARD_CONTENT_SQL = "SELECT prompt_json,system_json,response_json FROM call_content WHERE call_id=?"
AMBIENT_GUARD_PROBE_SQL = "SELECT 1"


class AmbientScopeLedgerOperationGuards(unittest.TestCase):
    def setUp(self):
        self.ambient_home = tempfile.TemporaryDirectory(dir=config.HOME)
        self.addCleanup(self.ambient_home.cleanup)
        self.ambient_path = pathlib.Path(self.ambient_home.name) / pathlib.Path(__file__).stem
        self.ambient_path_patch = patch.object(config, "db_path", return_value=self.ambient_path)
        self.ambient_path_patch.start()
        self.addCleanup(self.ambient_path_patch.stop)
        self.addCleanup(ledger_writer.close_ledger_writers)
        self.ambient_connection = sqlite3.connect(self.ambient_path, check_same_thread=False)
        self.addCleanup(self.ambient_connection.close)
        self.ambient_ledger = SpendLedger(self.ambient_path, owned_connection=self.ambient_connection)
        self.ambient_budget_patch = patch.object(budget._LEDGER_TL, "led", self.ambient_ledger, create=True)
        self.ambient_budget_patch.start()
        self.addCleanup(self.ambient_budget_patch.stop)
        self.ambient_today = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")
        self.ambient_event = {"kind": "realtime", "usd": AMBIENT_GUARD_AMOUNT,
                              "source": __name__, "day": self.ambient_today, "dedup_key": __name__}
        self.ambient_spool = self.ambient_path.with_suffix(".jsonl")

    def expire_ambient_operation_guard(self, scope):
        scope.deadline_at = time.monotonic() - AMBIENT_GUARD_EXPIRED_OFFSET_S

    def stage_ambient_operation_evidence(self):
        ledger_deadletter.append_spend_deadletter_event(self.ambient_spool, {"event": self.ambient_event})
        return self.ambient_spool.read_bytes()

    def count_ambient_operation_events(self):
        return self.ambient_connection.execute(AMBIENT_GUARD_COUNT_SQL, (__name__,)).fetchone()[0]

    def test_ambient_expired_budget_read_returns_committed_state(self):
        self.ambient_ledger.record_event(self.ambient_event)
        with ledger_read.ledger_request_scope() as ambient:
            self.expire_ambient_operation_guard(ambient)
            self.assertEqual(budget.spent_since(self.ambient_today), float(AMBIENT_GUARD_AMOUNT))
            self.assertIsNone(budget.exceeded())

    def test_ambient_expired_raw_write_lease_and_serialized_write(self):
        # Reserve before the request so the precise raw lease gate is exercised.
        with ledger_writer.admit_live_ledger_writer(str(self.ambient_path)) as owner:
            self.assertTrue(owner.ready.wait(timeout=config.LEDGER_WRITE_TIMEOUT_S))
            self.assertIsNone(owner.failure)
            with ledger_read.ledger_request_scope() as ambient:
                self.expire_ambient_operation_guard(ambient)
                with owner.cross_process_write_lease():
                    pass
                ledger_writer.run_ledger_write(
                    lambda: SpendLedger().record_event(self.ambient_event), self.ambient_path)
        self.assertEqual(self.count_ambient_operation_events(), 1)

    def test_ambient_expired_connection_open_returns_committed_state(self):
        with ledger_read.ledger_request_scope() as ambient:
            self.expire_ambient_operation_guard(ambient)
            connection = config.open_ledger_connection(self.ambient_path)
            try:
                self.assertEqual(connection.execute(AMBIENT_GUARD_PROBE_SQL).fetchone()[0], 1)
            finally:
                connection.close()
            connection = config.fresh_ledger_conn(lambda connection: None, self.ambient_path)
            try:
                self.assertEqual(connection.execute(AMBIENT_GUARD_PROBE_SQL).fetchone()[0], 1)
            finally:
                connection.close()

    def test_ambient_expired_content_retention_commits_complete_content(self):
        with patch.object(content_retention, "content_retention_enabled", return_value=True):
            with ledger_read.ledger_request_scope() as ambient:
                self.expire_ambient_operation_guard(ambient)
                content_id = content_retention.record_governed_call_content(
                    __name__, self.ambient_event, __name__, self.ambient_event)
        self.assertIsNotNone(content_id)
        stored = self.ambient_connection.execute(AMBIENT_GUARD_CONTENT_SQL, (__name__,)).fetchone()
        self.assertEqual(tuple(json.loads(decompress_retained_text(value)) for value in stored),
                         (self.ambient_event, __name__, self.ambient_event))

    def test_ambient_expired_deadletter_drain_commits_before_removal(self):
        self.stage_ambient_operation_evidence()
        with ledger_read.ledger_request_scope() as ambient:
            self.expire_ambient_operation_guard(ambient)
            _run_mcp_deadletter_drain((self.ambient_spool,), config.LEDGER_QUERY_DEADLINE_S)
        self.assertEqual(self.count_ambient_operation_events(), 1)
        self.assertEqual(self.ambient_spool.read_bytes(), b"")

    def test_ambient_cursor_new_statement_gets_fresh_operation_budget(self):
        with ledger_read.ledger_request_scope() as ambient:
            connection = config.open_ledger_connection(self.ambient_path)
            try:
                cursor = connection.execute(AMBIENT_GUARD_PROBE_SQL)
                self.expire_ambient_operation_guard(cursor._ledger_request_cursor_state.scope)
                self.expire_ambient_operation_guard(ambient)
                self.assertEqual(cursor.execute(AMBIENT_GUARD_PROBE_SQL).fetchone()[0], 1)
            finally:
                connection.close()

    def test_ambient_cursor_continuation_keeps_original_operation_expiry(self):
        with ledger_read.ledger_request_scope():
            connection = config.open_ledger_connection(self.ambient_path)
            try:
                cursor = connection.execute(AMBIENT_GUARD_PROBE_SQL)
                self.expire_ambient_operation_guard(cursor._ledger_request_cursor_state.scope)
                with self.assertRaises(ledger_read.LedgerQueryDeadline):
                    cursor.fetchone()
            finally:
                connection.close()

    def test_ambient_cancelled_write_retains_deadletter_evidence(self):
        evidence = self.stage_ambient_operation_evidence()
        with ledger_read.ledger_request_scope() as ambient:
            ambient.cancel_ledger_request()
            with self.assertRaises(ledger_read.LedgerRequestAbandoned):
                ledger_deadletter.drain_spend_deadletter_events((self.ambient_spool,))
        self.assertEqual(self.ambient_spool.read_bytes(), evidence)
        self.assertEqual(self.count_ambient_operation_events(), 0)

    def test_ambient_read_operation_progress_budget_still_expires(self):
        def exhaust_ambient_read_vm_budget(reader, *args, **kwargs):
            return reader._conn.execute(AMBIENT_GUARD_SLOW_SQL,
                                       (AMBIENT_GUARD_RECURSIVE_STEPS,)).fetchall()

        with patch.object(SpendLedger, "_cat_dec", exhaust_ambient_read_vm_budget):
            with self.assertRaises(ledger_read.LedgerQueryDeadline):
                self.ambient_ledger.spent_dec(deadline_s=AMBIENT_GUARD_NEGATIVE_BUDGET_S)

    def test_ambient_stuck_write_lease_expires_own_budget_and_retains_retry(self):
        evidence = self.stage_ambient_operation_evidence()
        ledger_writer.run_ledger_write(lambda: None, self.ambient_path)
        lock_path = pathlib.Path(str(self.ambient_path) + ledger_writer.LEDGER_WRITER_LOCK_SUFFIX)
        with lock_path.open("a") as blocker:
            fcntl.flock(blocker.fileno(), fcntl.LOCK_EX)
            try:
                with patch.object(config, "LEDGER_EVIDENCE_PERSISTENCE_BUDGET_S",
                                  AMBIENT_GUARD_NEGATIVE_BUDGET_S):
                    with ledger_read.ledger_request_scope() as ambient:
                        self.expire_ambient_operation_guard(ambient)
                        with self.assertRaises(ledger_read.LedgerQueryDeadline):
                            ledger_deadletter.drain_spend_deadletter_events((self.ambient_spool,))
            finally:
                fcntl.flock(blocker.fileno(), fcntl.LOCK_UN)
        self.assertEqual(self.ambient_spool.read_bytes(), evidence)
        self.assertEqual(self.count_ambient_operation_events(), 0)

    def test_ambient_write_operation_expiry_rolls_back_and_preserves_retry(self):
        evidence = self.stage_ambient_operation_evidence()
        original_record = SpendLedger.record_event

        def exhaust_ambient_write_operation(ledger, event, **kwargs):
            result = original_record(ledger, event, **kwargs)
            self.expire_ambient_operation_guard(ledger_read.current_ledger_request_scope())
            return result

        with patch.object(SpendLedger, "record_event", exhaust_ambient_write_operation):
            with self.assertRaises(ledger_read.LedgerQueryDeadline):
                ledger_deadletter.drain_spend_deadletter_events((self.ambient_spool,))
        self.assertEqual(self.ambient_spool.read_bytes(), evidence)
        self.assertEqual(self.count_ambient_operation_events(), 0)


if __name__ == "__main__":
    unittest.main()
