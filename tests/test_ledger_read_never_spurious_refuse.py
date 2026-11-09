"""Committed cap/dispatch evidence survives unrelated writer/request lifetimes."""
import datetime
import contextlib
import os
import pathlib
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from _pytest_fixture_bridge import align_test_startup_home

align_test_startup_home(prefix=__name__)

from spendguard import budget, config, delegate_router, ledger_read
from spendguard.ledger import SpendLedger, SPEND_EVENT_NANO_COLUMNS
from spendguard.money_scale import usd_to_nano_usd
from spendguard.store.configuration import ledger_connection

READ_REFUSAL_COMMITTED_USD = "1.125000001"
READ_REFUSAL_PENDING_USD = "9.0"
READ_REFUSAL_INVOICE_USD = "2.0"
READ_REFUSAL_EST_VALUE_USD = "4.0"
READ_REFUSAL_LANE = "test-read-snapshot-lane"
READ_REFUSAL_ESTIMATE_USD = 1.0
READ_REFUSAL_TOKEN_COUNT = 1
READ_REFUSAL_INTERRUPT_VM_INSTRUCTIONS = 1
READ_REFUSAL_INVOICE_RATIO = float(READ_REFUSAL_INVOICE_USD) / float(READ_REFUSAL_EST_VALUE_USD)
READ_REFUSAL_EVIDENCE_TOTALS = (float(READ_REFUSAL_INVOICE_USD), float(READ_REFUSAL_EST_VALUE_USD))
READ_REFUSAL_MODEL = "test-read-snapshot-model"
READ_REFUSAL_TEST_BUDGET_S = config.LEDGER_QUERY_DEADLINE_S
READ_REFUSAL_FAST_FRACTION = 0.5
READ_REFUSAL_EXPIRED_OFFSET_S = 1.0
READ_REFUSAL_NEGATIVE_BUDGET_S = 0.02
READ_REFUSAL_RECURSIVE_STEPS = 1000000000
READ_REFUSAL_SLOW_SQL = """WITH RECURSIVE work(value) AS
(VALUES(0) UNION ALL SELECT value+1 FROM work WHERE value<?)
SELECT SUM(value) FROM work"""
READ_REFUSAL_PENDING_UPDATE_SQL = (
    f"UPDATE spend_events SET realtime_usd=?, {SPEND_EVENT_NANO_COLUMNS['realtime_usd']}=? WHERE source=?")
READ_REFUSAL_CONTENTION_POOL_KEY = "ledger-read-contention-writer"
READ_REFUSAL_COLD_PROCESS_TEST = (
    "LedgerReadNeverSpuriousRefuseGuards."
    "test_saturated_writer_pool_does_not_delay_independent_read_snapshot")
READ_REFUSAL_SUBPROCESS_BUDGET_MULTIPLIER = 2
READ_REFUSAL_HOME_ENVIRONMENT_VARIABLE = "SPENDGUARD_HOME"


class LedgerReadNeverSpuriousRefuseGuards(unittest.TestCase):
    def setUp(self):
        self.home_owner = tempfile.TemporaryDirectory(dir=config.HOME)
        self.addCleanup(self.home_owner.cleanup)
        self.path = pathlib.Path(self.home_owner.name) / pathlib.Path(__file__).stem
        self.path_patch = patch.object(config, "db_path", return_value=self.path)
        self.path_patch.start()
        self.addCleanup(self.path_patch.stop)
        self.connection = sqlite3.connect(self.path, check_same_thread=False)
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.ledger = SpendLedger(self.path, owned_connection=self.connection)
        self.addCleanup(self.connection.close)
        self.cached_patch = patch.object(budget._LEDGER_TL, "led", self.ledger, create=True)
        self.cached_patch.start()
        self.addCleanup(self.cached_patch.stop)
        self.today = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")
        self.ledger.record_event({"kind": "realtime", "usd": READ_REFUSAL_COMMITTED_USD,
                                  "source": __name__, "occurred_at": self.today,
                                  "day": self.today})

        self.ledger.record_event({"kind": "realtime", "usd": READ_REFUSAL_INVOICE_USD,
                                  "source": ledger_read.INVOICE_OVERAGE_SOURCE,
                                  "intent": ledger_read.INVOICE_OVERAGE_INTENT_PATTERN.removesuffix("%") + __name__, "is_meta": True,
                                  "occurred_at": self.today, "day": self.today})
        self.ledger.record_event({"kind": "est_chat", "usd": READ_REFUSAL_EST_VALUE_USD,
                                  "source": ledger_read.DELEGATE_PLAN_SOURCE,
                                  "conv_id": __name__, "occurred_at": self.today, "day": self.today})

    def read_dispatch_admission_estimate(self):
        classification = {"provider": READ_REFUSAL_LANE, "kind": "oneshot", "self_contained": True}
        with contextlib.ExitStack() as stack:
            stack.enter_context(patch.object(delegate_router, "classify_task", return_value=classification))
            stack.enter_context(patch.object(delegate_router, "delegation_lanes_ready",
                                             return_value={"ready": [READ_REFUSAL_LANE]}))
            stack.enter_context(patch.object(delegate_router, "_model_for_plan", return_value=READ_REFUSAL_MODEL))
            stack.enter_context(patch.object(delegate_router.lane_registry, "lane_spec",
                                             return_value={"provider": READ_REFUSAL_LANE}))
            stack.enter_context(patch.object(delegate_router, "_estimated_tokens", return_value=(READ_REFUSAL_TOKEN_COUNT, READ_REFUSAL_TOKEN_COUNT, __name__)))
            stack.enter_context(patch.object(delegate_router.pricing, "realtime_cost", return_value=READ_REFUSAL_ESTIMATE_USD))
            result = delegate_router.delegate_task(__name__)
            self.assertEqual(result["status"], "estimate")
            return result["estimate"]["saved_overage_usd"]

    def read_overage_totals(self):
        invoices, value = ledger_read.read_delegate_overage_evidence()
        return sum(row["dollars"] for row in invoices), value

    def read_attribution_totals(self):
        real, windows, fallback = ledger_read.read_cc_attribution_evidence()
        return sum(row["billed"] for row in real), sum(row["est"] for row in fallback)

    def read_central_sql_amount(self):
        request = ledger_read.LedgerSQLReadRequest(
            "SELECT realtime_usd FROM spend_events WHERE source=?", (__name__,))
        return ledger_read.read_ledger_sql(request, self.path)[0]["realtime_usd"]

    def read_bulk_chunked_committed_amount(self):
        with ledger_read._ledger_read_connection(self.path, bulk_chunked=True) as reader:
            return reader.execute("SELECT realtime_usd FROM spend_events WHERE source=?",
                                  (__name__,)).fetchone()[0]

    def committed_read_cases(self):
        return (
            ("bulk_chunked", self.read_bulk_chunked_committed_amount, READ_REFUSAL_COMMITTED_USD),
            ("central_sql", self.read_central_sql_amount, READ_REFUSAL_COMMITTED_USD),
            ("spent_dec", lambda: self.ledger.spent_dec(), READ_REFUSAL_COMMITTED_USD),
            ("running_llm_total_dec", lambda: self.ledger.running_llm_total_dec(), READ_REFUSAL_COMMITTED_USD),
            ("spent_since", lambda: budget.spent_since(self.today), float(READ_REFUSAL_COMMITTED_USD)),
            ("spent_since_read_only", lambda: budget.spent_since(self.today, read_only=True), float(READ_REFUSAL_COMMITTED_USD)),
            ("exceeded", lambda: budget.exceeded(), None),
            ("dispatch_admission", self.read_dispatch_admission_estimate, READ_REFUSAL_ESTIMATE_USD * READ_REFUSAL_INVOICE_RATIO),
            ("dispatch_overage", delegate_router._measured_claude_overage_ratio, READ_REFUSAL_INVOICE_RATIO),
            ("dispatch_overage_evidence", self.read_overage_totals, READ_REFUSAL_EVIDENCE_TOTALS),
            ("dispatch_attribution", self.read_attribution_totals, READ_REFUSAL_EVIDENCE_TOTALS),
        )

    def assert_committed_reads_are_fast(self):
        for name, read, expected in self.committed_read_cases():
            with self.subTest(read=name):
                started = time.monotonic()
                self.assertEqual(read(), expected)
                self.assertLess(time.monotonic() - started,
                                READ_REFUSAL_TEST_BUDGET_S * READ_REFUSAL_FAST_FRACTION)

    def test_plain_writer_transaction_reads_last_committed_snapshot(self):
        self.connection.execute("BEGIN")
        self.connection.execute(READ_REFUSAL_PENDING_UPDATE_SQL,
                                (READ_REFUSAL_PENDING_USD, usd_to_nano_usd(READ_REFUSAL_PENDING_USD), __name__))
        try:
            self.assert_committed_reads_are_fast()
            self.assertTrue(self.connection.in_transaction)
            self.assertEqual(self.connection.execute("SELECT realtime_usd FROM spend_events").fetchone()[0],
                             READ_REFUSAL_PENDING_USD)
        finally:
            self.connection.rollback()

    def test_saturated_writer_pool_does_not_delay_independent_read_snapshot(self):
        original_pool_settings = ledger_connection.ledger_read_pool_settings

        def ensure_contention_ledger_schema(connection):
            connection.execute("SELECT 1").close()

        writer = ledger_connection.pooled_ledger_conn(
            READ_REFUSAL_CONTENTION_POOL_KEY, ensure_contention_ledger_schema, self.path)
        saturated_pool_cap = sum(
            not handle._ledger_released for handle in ledger_connection._LEDGER_POOL_REGISTRY)

        def contention_pool_settings():
            _, admission_timeout = original_pool_settings()
            return saturated_pool_cap, admission_timeout

        writer.execute("BEGIN IMMEDIATE")
        writer.execute(READ_REFUSAL_PENDING_UPDATE_SQL,
                       (READ_REFUSAL_PENDING_USD, usd_to_nano_usd(READ_REFUSAL_PENDING_USD), __name__))
        try:
            with patch.object(ledger_connection, "ledger_read_pool_settings", side_effect=contention_pool_settings):
                started = time.monotonic()
                self.assertEqual(self.read_central_sql_amount(), READ_REFUSAL_COMMITTED_USD)
                self.assertLess(time.monotonic() - started,
                                ledger_read.DEFAULT_LEDGER_READ_TIME_BUDGET_S)
        finally:
            writer.rollback()
            ledger_connection.rollback_ledger_conn(READ_REFUSAL_CONTENTION_POOL_KEY)
            ledger_connection.reset_ledger_conn(READ_REFUSAL_CONTENTION_POOL_KEY)

    def test_writer_absent_independent_read_snapshot_control(self):
        started = time.monotonic()
        self.assertEqual(self.read_central_sql_amount(), READ_REFUSAL_COMMITTED_USD)
        self.assertLess(time.monotonic() - started,
                        ledger_read.DEFAULT_LEDGER_READ_TIME_BUDGET_S)

    def test_cold_process_first_read_survives_saturated_writer_pool(self):
        with tempfile.TemporaryDirectory() as cold_process_home:
            cold_process_environment = os.environ.copy()
            cold_process_environment[READ_REFUSAL_HOME_ENVIRONMENT_VARIABLE] = cold_process_home
            completed = subprocess.run(
                [sys.executable, __file__, READ_REFUSAL_COLD_PROCESS_TEST],
                capture_output=True, text=True, check=False, env=cold_process_environment,
                timeout=(ledger_read.DEFAULT_LEDGER_READ_TIME_BUDGET_S
                         * READ_REFUSAL_SUBPROCESS_BUDGET_MULTIPLIER))
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)

    def test_expired_ambient_request_does_not_consume_query_budget(self):
        with ledger_read.ledger_request_scope() as scope:
            scope.deadline_at = time.monotonic() - READ_REFUSAL_EXPIRED_OFFSET_S
            self.assert_committed_reads_are_fast()

    def test_corrupt_snapshot_never_returns_a_cap_total(self):
        corrupt_path = self.path.with_suffix(".corrupt")
        corrupt_path.write_bytes(b"deliberately invalid sqlite database for negative control")
        with patch.object(config, "db_path", return_value=corrupt_path):
            with self.assertRaises(ledger_read.LedgerQueryDeadline):
                budget.spent_since(self.today)

    def test_cancelled_transport_still_refuses(self):
        with ledger_read.ledger_request_scope() as scope:
            scope.cancel_ledger_request()
            with self.assertRaises(ledger_read.LedgerRequestAbandoned):
                self.ledger.spent_dec()

    def test_parent_cancellation_interrupts_active_snapshot(self):
        with ledger_read.ledger_request_scope() as parent:
            def run_parent_cancelled_query(reader, *args, **kwargs):
                reader._conn.create_function("cancel_parent_read", 0, parent.cancel_ledger_request)
                return reader._conn.execute("SELECT cancel_parent_read()").fetchone()

            with patch.object(SpendLedger, "_cat_dec", run_parent_cancelled_query):
                with self.assertRaises(ledger_read.LedgerRequestAbandoned):
                    self.ledger.spent_dec()

    def test_sqlite_progress_interruption_still_refuses(self):
        def run_interrupted_snapshot_query(reader, *args, **kwargs):
            reader._conn.set_progress_handler(lambda: True, READ_REFUSAL_INTERRUPT_VM_INSTRUCTIONS)
            return reader._conn.execute(READ_REFUSAL_SLOW_SQL,
                                        (READ_REFUSAL_RECURSIVE_STEPS,)).fetchall()

        with patch.object(SpendLedger, "_cat_dec", run_interrupted_snapshot_query):
            with self.assertRaises(ledger_read.LedgerQueryDeadline):
                self.ledger.spent_dec()

    def test_real_query_progress_deadline_still_refuses(self):
        def run_over_budget_query(reader, *args, **kwargs):
            reader._conn.execute(READ_REFUSAL_SLOW_SQL,
                                 (READ_REFUSAL_RECURSIVE_STEPS,)).fetchall()
            raise AssertionError("negative control query unexpectedly completed")

        with patch.object(SpendLedger, "_cat_dec", run_over_budget_query):
            with self.assertRaises(ledger_read.LedgerQueryDeadline):
                self.ledger.spent_dec(deadline_s=READ_REFUSAL_NEGATIVE_BUDGET_S)


if __name__ == "__main__":
    unittest.main()
