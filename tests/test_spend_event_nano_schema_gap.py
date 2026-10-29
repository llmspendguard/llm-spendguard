"""Offline guards for calls-open migration and pre-migration spend readers."""
import sqlite3
from decimal import Decimal

import pytest

from _pytest_fixture_bridge import align_test_startup_home, run_pytest_style

align_test_startup_home(prefix=__name__)

from spendguard import calls, ledger, ledger_read
from spendguard.money_scale import nano_usd_to_usd, usd_to_nano_usd

SPEND_GAP_DB_NAME = 'spend-event-schema-gap.sqlite'
SPEND_GAP_AMOUNT = '0.125'
SPEND_GAP_REVISED = '0.875'
SPEND_GAP_ROUNDING_AMOUNTS = ('0.0000000025', '-0.0000000035', None)
SPEND_GAP_DIMENSIONS = ('fixture-provider', 'fixture-day', 'fixture-intent', 'fixture-model')


def create_spend_gap_legacy_database(tmp_path):
    path = tmp_path / SPEND_GAP_DB_NAME
    connection = sqlite3.connect(path)
    connection.execute(ledger._DDL_EVENTS)
    columns = ('provider', 'day', 'intent', 'model', *ledger.SPEND_EVENT_NANO_COLUMNS)
    connection.execute(f"INSERT INTO spend_events({','.join(columns)}) VALUES({','.join('?' for _ in columns)})",
                       (*SPEND_GAP_DIMENSIONS, *((SPEND_GAP_AMOUNT,) * len(ledger.SPEND_EVENT_NANO_COLUMNS))))
    connection.commit()
    return path, connection


def assert_spend_gap_reader_amounts(path, expected):
    row = ledger_read.read_spend_aggregates(path=path)[0]
    for column in ledger.USD_COLS:
        assert row[column] == Decimal(expected)
    provider, day, intent, model = SPEND_GAP_DIMENSIONS
    assert ledger_read.read_represented_call_cell(intent, model, day, path=path) == float(
        Decimal(expected) * len(ledger.LLM_USD_COLS))


def test_spend_gap_legacy_readers_return_without_schema_error(tmp_path):
    path, connection = create_spend_gap_legacy_database(tmp_path)
    try:
        assert_spend_gap_reader_amounts(path, SPEND_GAP_AMOUNT)
    finally:
        connection.close()


def test_spend_gap_initializer_backfills_every_column_idempotently(tmp_path):
    path, connection = create_spend_gap_legacy_database(tmp_path)
    try:
        ledger._initialize_spend_event_nano_schema(connection)
        available = {row[1] for row in connection.execute('PRAGMA table_info(spend_events)')}
        assert set(ledger.SPEND_EVENT_NANO_COLUMNS.values()) <= available
        for legacy_column, nano_column in ledger.SPEND_EVENT_NANO_COLUMNS.items():
            raw, nano = connection.execute(f'SELECT {legacy_column},{nano_column} FROM spend_events').fetchone()
            assert nano == usd_to_nano_usd(raw)
            assert nano_usd_to_usd(nano) == Decimal(raw)
        before = connection.total_changes
        ledger._initialize_spend_event_nano_schema(connection)
        assert connection.total_changes == before
    finally:
        connection.close()


def test_spend_gap_migrated_readers_prefer_nano_and_fall_back_on_null(tmp_path):
    path, connection = create_spend_gap_legacy_database(tmp_path)
    try:
        ledger._initialize_spend_event_nano_schema(connection)
        for nano_column in ledger.SPEND_EVENT_NANO_COLUMNS.values():
            connection.execute(f'UPDATE spend_events SET {nano_column}=?', (usd_to_nano_usd(SPEND_GAP_REVISED),))
        connection.commit()
        assert_spend_gap_reader_amounts(path, SPEND_GAP_REVISED)
        for nano_column in ledger.SPEND_EVENT_NANO_COLUMNS.values():
            connection.execute(f'UPDATE spend_events SET {nano_column}=NULL')
        connection.commit()
        assert_spend_gap_reader_amounts(path, SPEND_GAP_AMOUNT)
    finally:
        connection.close()


def test_spend_gap_initializer_uses_shared_rounding_and_preserves_populated_nano(tmp_path):
    path, connection = create_spend_gap_legacy_database(tmp_path)
    try:
        columns = tuple(ledger.SPEND_EVENT_NANO_COLUMNS)
        for amount in SPEND_GAP_ROUNDING_AMOUNTS:
            connection.execute(
                f"INSERT INTO spend_events({','.join(columns)}) VALUES({','.join('?' for _ in columns)})",
                (amount,) * len(columns))
        ledger._initialize_spend_event_nano_schema(connection)
        for legacy_column, nano_column in ledger.SPEND_EVENT_NANO_COLUMNS.items():
            for raw, nano in connection.execute(f'SELECT {legacy_column},{nano_column} FROM spend_events'):
                assert nano == (None if raw is None else usd_to_nano_usd(raw))
            connection.execute(f'UPDATE spend_events SET {nano_column}=?', (usd_to_nano_usd(SPEND_GAP_REVISED),))
        before = connection.total_changes
        ledger._initialize_spend_event_nano_schema(connection)
        assert connection.total_changes == before
    finally:
        connection.close()


def test_spend_gap_arithmetic_projection_keeps_wildcards_rejected(tmp_path):
    path, connection = create_spend_gap_legacy_database(tmp_path)
    try:
        for projection in ('*', 'spend_events.*'):
            with pytest.raises(ValueError, match='explicit column projections'):
                ledger_read.read_ledger_sql(ledger_read.LedgerSQLReadRequest(
                    f'SELECT {projection} FROM spend_events'), path)
        assert_spend_gap_reader_amounts(path, SPEND_GAP_AMOUNT)
    finally:
        connection.close()


def test_spend_gap_calls_open_repairs_existing_spend_events(tmp_path):
    path, connection = create_spend_gap_legacy_database(tmp_path)
    try:
        calls._ensure_call_observation_schema(connection)
        for nano_column in ledger.SPEND_EVENT_NANO_COLUMNS.values():
            assert connection.execute(f'SELECT {nano_column} FROM spend_events').fetchone()[0] == usd_to_nano_usd(
                SPEND_GAP_AMOUNT)
        assert_spend_gap_reader_amounts(path, SPEND_GAP_AMOUNT)
    finally:
        connection.close()


if __name__ == '__main__':
    raise SystemExit(run_pytest_style(globals()))
