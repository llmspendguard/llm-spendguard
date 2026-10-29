"""Offline guards against consuming an incomplete settlement as cheap evidence."""
import os
import sqlite3
import tempfile
from pathlib import Path
from contextlib import contextmanager
from unittest.mock import patch

os.environ.setdefault("SPENDGUARD_TEST_ISOLATED", "1")
os.environ.setdefault("SPENDGUARD_HOME", tempfile.mkdtemp())

from spendguard import ledger_read, lane_quota_cost

SETTLEMENT_GUARD_DATABASE_NAME = "settlement-read.sqlite"
SETTLEMENT_GUARD_NANO_DATABASE_NAME = "settlement-nano-read.sqlite"
SETTLEMENT_GUARD_LEGACY_DATABASE_NAME = "legacy-read.sqlite"
SETTLEMENT_GUARD_INPUT_FLOOR = 0.25
SETTLEMENT_GUARD_INPUT_FLOOR_NANO = int(
    SETTLEMENT_GUARD_INPUT_FLOOR * ledger_read.NANO_USD_PER_USD)
SETTLEMENT_GUARD_SELECTED_TIME = "2026-01-01"
SETTLEMENT_GUARD_PROJECT = "settlement-guard"
SETTLEMENT_GUARD_SCHEMA = """CREATE TABLE calls (
cost REAL, out_tok INTEGER, settlement_complete INTEGER,
input_cost_floor REAL, ts TEXT, project TEXT)"""
SETTLEMENT_GUARD_NANO_SCHEMA = f"""CREATE TABLE calls (
cost REAL, out_tok INTEGER, settlement_complete INTEGER,
{ledger_read.CALL_INPUT_COST_FLOOR_NANO_COLUMN} INTEGER, ts TEXT, project TEXT)"""
SETTLEMENT_GUARD_QUERIES = (
    "SELECT SUM(cost) FROM calls",
    "SELECT COUNT(*) FROM calls",
    "SELECT AVG(out_tok) FROM calls WHERE out_tok>0",
    "SELECT SUM(cost) FROM calls WHERE cost>0",
    "WITH paid AS (SELECT cost FROM calls WHERE cost>0) SELECT SUM(cost) FROM paid",
    "SELECT cost FROM calls WHERE settlement_complete=1",
    "SELECT SUM(saved_usd) FROM decisions",
    "SELECT actual_usd FROM decisions WHERE actual_usd>0",
)


def test_incomplete_settlement_sql_cannot_hide_unknowns():
    with tempfile.TemporaryDirectory() as settlement_guard_directory:
        settlement_guard_path = Path(settlement_guard_directory) / SETTLEMENT_GUARD_DATABASE_NAME
        with sqlite3.connect(settlement_guard_path) as settlement_guard_writer:
            settlement_guard_writer.execute(SETTLEMENT_GUARD_SCHEMA)
            settlement_guard_writer.execute("CREATE TABLE decisions(actual_usd REAL,saved_usd REAL)")
            settlement_guard_writer.execute("INSERT INTO decisions VALUES (NULL,NULL)")
            settlement_guard_writer.execute(
                "INSERT INTO calls VALUES (NULL,NULL,0,?,?,?)",
                (SETTLEMENT_GUARD_INPUT_FLOOR, SETTLEMENT_GUARD_SELECTED_TIME,
                 SETTLEMENT_GUARD_PROJECT))
        for settlement_guard_query in SETTLEMENT_GUARD_QUERIES:
            try:
                ledger_read.read_ledger_sql(
                    ledger_read.LedgerSQLReadRequest(settlement_guard_query), settlement_guard_path)
            except ledger_read.LedgerSettlementIncomplete as settlement_guard_error:
                assert settlement_guard_error.incomplete_count == 1
                assert settlement_guard_error.input_cost_floor == SETTLEMENT_GUARD_INPUT_FLOOR
            else:
                raise AssertionError(f"Incomplete evidence accepted: {settlement_guard_query}")
        settlement_guard_metadata = ledger_read.read_incomplete_call_settlements(
            SETTLEMENT_GUARD_SELECTED_TIME, SETTLEMENT_GUARD_PROJECT, settlement_guard_path)
        assert settlement_guard_metadata == {"n": 1, "input_cost_floor": SETTLEMENT_GUARD_INPUT_FLOOR}
        assert ledger_read.read_ledger_sql(
            ledger_read.LedgerSQLReadRequest("SELECT 1 AS independent"), settlement_guard_path)[0][0] == 1


def test_incomplete_settlement_nano_floor_cannot_hide_unknowns():
    with tempfile.TemporaryDirectory() as settlement_nano_directory:
        settlement_nano_path = Path(settlement_nano_directory) / SETTLEMENT_GUARD_NANO_DATABASE_NAME
        with sqlite3.connect(settlement_nano_path) as settlement_nano_writer:
            settlement_nano_writer.execute(SETTLEMENT_GUARD_NANO_SCHEMA)
            settlement_nano_writer.execute(
                "INSERT INTO calls VALUES (NULL,NULL,0,?,?,?)",
                (SETTLEMENT_GUARD_INPUT_FLOOR_NANO, SETTLEMENT_GUARD_SELECTED_TIME,
                 SETTLEMENT_GUARD_PROJECT))
        try:
            ledger_read.read_ledger_sql(
                ledger_read.LedgerSQLReadRequest("SELECT SUM(cost) FROM calls"),
                settlement_nano_path)
        except ledger_read.LedgerSettlementIncomplete as settlement_nano_error:
            assert settlement_nano_error.incomplete_count == 1
            assert settlement_nano_error.input_cost_floor == SETTLEMENT_GUARD_INPUT_FLOOR
        else:
            raise AssertionError("Incomplete nano evidence accepted")
        settlement_nano_metadata = ledger_read.read_incomplete_call_settlements(
            SETTLEMENT_GUARD_SELECTED_TIME, SETTLEMENT_GUARD_PROJECT, settlement_nano_path)
        assert settlement_nano_metadata == {
            "n": 1, "input_cost_floor": SETTLEMENT_GUARD_INPUT_FLOOR}


def test_legacy_settlement_schema_is_not_guessed():
    with tempfile.TemporaryDirectory() as settlement_legacy_directory:
        settlement_legacy_path = Path(settlement_legacy_directory) / SETTLEMENT_GUARD_LEGACY_DATABASE_NAME
        with sqlite3.connect(settlement_legacy_path) as settlement_legacy_writer:
            settlement_legacy_writer.execute("CREATE TABLE calls(cost REAL)")
            settlement_legacy_writer.execute("INSERT INTO calls VALUES (?)", (SETTLEMENT_GUARD_INPUT_FLOOR,))
        assert ledger_read.read_recorded_call_total(settlement_legacy_path) == SETTLEMENT_GUARD_INPUT_FLOOR
        assert ledger_read.read_incomplete_call_settlements(path=settlement_legacy_path) == {
            "n": 0, "input_cost_floor": None}


def test_quota_absent_schema_classification_uses_original_snapshot():
    with tempfile.TemporaryDirectory() as quota_schema_directory:
        quota_schema_path = Path(quota_schema_directory) / SETTLEMENT_GUARD_DATABASE_NAME
        sqlite3.connect(quota_schema_path).close()
        original_quota_read_connection = ledger_read._ledger_read_connection
        quota_schema_created = False

        @contextmanager
        def create_quota_schema_after_read_snapshot(*args, **kwargs):
            nonlocal quota_schema_created
            try:
                with original_quota_read_connection(*args, **kwargs) as quota_snapshot_connection:
                    yield quota_snapshot_connection
            finally:
                if not quota_schema_created:
                    quota_schema_created = True
                    with sqlite3.connect(quota_schema_path) as quota_schema_writer:
                        quota_schema_writer.execute(SETTLEMENT_GUARD_SCHEMA)

        with patch.object(ledger_read.config, "db_path", return_value=quota_schema_path), \
             patch.object(ledger_read, "_ledger_read_connection", create_quota_schema_after_read_snapshot):
            assert lane_quota_cost.measured_lane_token_cost() == {}
        # A present table missing quota columns is a schema failure, never
        # confused with a database whose calls table has not yet been created.
        with patch.object(ledger_read.config, "db_path", return_value=quota_schema_path):
            try:
                lane_quota_cost.measured_lane_token_cost()
            except ledger_read.LedgerReadSchemaError:
                pass
            else:
                raise AssertionError("Malformed quota schema must remain an explicit failure")


if __name__ == "__main__":
    test_incomplete_settlement_sql_cannot_hide_unknowns()
    test_incomplete_settlement_nano_floor_cannot_hide_unknowns()
    test_legacy_settlement_schema_is_not_guessed()
    test_quota_absent_schema_classification_uses_original_snapshot()
    print("4 incomplete settlement reader guards passed")
