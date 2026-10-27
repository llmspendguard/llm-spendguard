"""Reserved reports retain outer writes and clean up their own snapshot state."""
import pathlib
import tempfile
import unittest
from contextlib import closing

from _pytest_fixture_bridge import align_test_startup_home

align_test_startup_home(prefix=__name__)

from spendguard import config, ledger_read
from spendguard.ledger_writer import bind_reserved_ledger_bulk_reads

RESERVED_SETTLEMENT_SCHEMA_SQL = """CREATE TABLE calls (
    in_tok INTEGER,out_tok INTEGER,token_usage_complete INTEGER,
    settlement_complete INTEGER,input_cost_floor REAL,cost REAL)"""
RESERVED_SETTLEMENT_INSERT_SQL = "INSERT INTO calls VALUES (?,?,?,?,?,?)"
RESERVED_SETTLEMENT_COMPLETE_ROW = (1, 7, 0, 1, None, 0)
RESERVED_SETTLEMENT_UNKNOWN_ROW = (1, None, 0, 0, 0.25, None)
RESERVED_SETTLEMENT_REPORT_SQL = """SELECT
    CASE WHEN COUNT(out_tok)=COUNT(*) THEN SUM(out_tok) END AS output,
    SUM(cost) AS cost FROM calls"""
RESERVED_SETTLEMENT_TEMP_SQL = "SELECT COUNT(*) FROM sqlite_temp_master WHERE name='calls'"
RESERVED_SETTLEMENT_COUNT_SQL = "SELECT COUNT(*) FROM main.calls"
RESERVED_SETTLEMENT_REPETITIONS = 2
RESERVED_SETTLEMENT_TIMEOUT_BUDGET_S = 0.05
RESERVED_SETTLEMENT_RECURSIVE_STEPS = 1000000000
RESERVED_SETTLEMENT_PROGRESS_INTERVAL = 100
RESERVED_SETTLEMENT_SLOW_SQL = """WITH RECURSIVE reserved_steps(value) AS
    (VALUES(0) UNION ALL SELECT value+1 FROM reserved_steps WHERE value<?)
    SELECT SUM(value) FROM reserved_steps"""


class ReservedSettlementCompositionGuards(unittest.TestCase):
    def test_reserved_settlement_deadline_restores_owner_without_losing_outer_writes(self):
        with tempfile.TemporaryDirectory() as reserved_deadline_directory:
            reserved_deadline_path = pathlib.Path(reserved_deadline_directory) / pathlib.Path(__file__).stem
            with closing(config.open_ledger_connection(reserved_deadline_path)) as reserved_deadline_connection:
                reserved_deadline_connection.execute(RESERVED_SETTLEMENT_SCHEMA_SQL)
                reserved_deadline_connection.commit()
                reserved_deadline_connection.execute(
                    RESERVED_SETTLEMENT_INSERT_SQL, RESERVED_SETTLEMENT_COMPLETE_ROW)
                reserved_progress_calls = []

                def observe_reserved_prior_progress():
                    reserved_progress_calls.append(None)
                    return False

                reserved_deadline_connection.set_progress_handler(
                    observe_reserved_prior_progress, RESERVED_SETTLEMENT_PROGRESS_INTERVAL)
                for reserved_prior_owner in (False, True):
                    with self.subTest(prior_owner=reserved_prior_owner):
                        reserved_deadline_connection._ledger_owned_read_deadline = reserved_prior_owner
                        with bind_reserved_ledger_bulk_reads(reserved_deadline_path, reserved_deadline_connection):
                            with self.assertRaises(ledger_read.LedgerQueryDeadline):
                                ledger_read.read_ledger_sql(ledger_read.LedgerSQLReadRequest(
                                    RESERVED_SETTLEMENT_SLOW_SQL,
                                    parameters=(RESERVED_SETTLEMENT_RECURSIVE_STEPS,),
                                    time_budget=RESERVED_SETTLEMENT_TIMEOUT_BUDGET_S), reserved_deadline_path)
                        self.assertTrue(reserved_progress_calls)
                        self.assertEqual(reserved_deadline_connection._ledger_progress,
                                         (observe_reserved_prior_progress, RESERVED_SETTLEMENT_PROGRESS_INTERVAL))
                        self.assertEqual(reserved_deadline_connection._ledger_owned_read_deadline, reserved_prior_owner)
                        self.assertTrue(reserved_deadline_connection.in_transaction)
                        self.assertEqual(reserved_deadline_connection.execute(
                            RESERVED_SETTLEMENT_COUNT_SQL).fetchone()[0], 1)
                        self.assertEqual(reserved_deadline_connection.execute(
                            RESERVED_SETTLEMENT_TEMP_SQL).fetchone()[0], 0)
                reserved_deadline_connection._ledger_owned_read_deadline = False
                reserved_deadline_connection.rollback()
                self.assertEqual(reserved_deadline_connection.execute(
                    RESERVED_SETTLEMENT_COUNT_SQL).fetchone()[0], 0)

    def test_reserved_settlement_snapshot_preserves_outer_transaction_and_repeats(self):
        for reserved_outer_active in (False, True):
            with self.subTest(outer_active=reserved_outer_active), tempfile.TemporaryDirectory() as reserved_directory:
                reserved_path = pathlib.Path(reserved_directory) / pathlib.Path(__file__).stem
                with closing(config.open_ledger_connection(reserved_path)) as reserved_connection:
                    reserved_connection.execute(RESERVED_SETTLEMENT_SCHEMA_SQL)
                    reserved_connection.commit()
                    reserved_connection.execute(RESERVED_SETTLEMENT_INSERT_SQL, RESERVED_SETTLEMENT_COMPLETE_ROW)
                    if not reserved_outer_active:
                        reserved_connection.commit()
                    with bind_reserved_ledger_bulk_reads(reserved_path, reserved_connection):
                        for _ in range(RESERVED_SETTLEMENT_REPETITIONS):
                            reserved_result = ledger_read.read_ledger_sql(
                                ledger_read.LedgerSQLReadRequest(RESERVED_SETTLEMENT_REPORT_SQL), reserved_path)
                            self.assertIsNone(reserved_result[0][0])
                            self.assertEqual(reserved_result[0][1], RESERVED_SETTLEMENT_COMPLETE_ROW[-1])
                            self.assertEqual(reserved_connection.in_transaction, reserved_outer_active)
                            self.assertEqual(reserved_connection.execute(RESERVED_SETTLEMENT_TEMP_SQL).fetchone()[0], 0)
                        reserved_connection.execute(RESERVED_SETTLEMENT_INSERT_SQL, RESERVED_SETTLEMENT_UNKNOWN_ROW)
                        for _ in range(RESERVED_SETTLEMENT_REPETITIONS):
                            with self.assertRaises(ledger_read.LedgerSettlementIncomplete):
                                ledger_read.read_ledger_sql(
                                    ledger_read.LedgerSQLReadRequest(RESERVED_SETTLEMENT_REPORT_SQL), reserved_path)
                            self.assertTrue(reserved_connection.in_transaction)
                            self.assertEqual(reserved_connection.execute(RESERVED_SETTLEMENT_TEMP_SQL).fetchone()[0], 0)
                            self.assertEqual(reserved_connection.execute(RESERVED_SETTLEMENT_COUNT_SQL).fetchone()[0],
                                             len((RESERVED_SETTLEMENT_COMPLETE_ROW, RESERVED_SETTLEMENT_UNKNOWN_ROW)))
                    reserved_connection.rollback()
                    self.assertEqual(reserved_connection.execute(RESERVED_SETTLEMENT_COUNT_SQL).fetchone()[0],
                                     0 if reserved_outer_active else 1)


if __name__ == "__main__":
    unittest.main()
