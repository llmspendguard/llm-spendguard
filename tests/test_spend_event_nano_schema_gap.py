"""Offline guards for calls-open migration and pre-migration spend readers."""
import sqlite3
import socket
import time
from decimal import Decimal
from unittest.mock import patch

import pytest

from _pytest_fixture_bridge import align_test_startup_home, run_pytest_style

align_test_startup_home(prefix=__name__)

from spendguard import calls, config, ledger, ledger_read
from spendguard.money_scale import nano_usd_to_usd, real_usd_to_nano_usd, usd_to_nano_usd

SPEND_GAP_DB_NAME = 'spend-event-schema-gap.sqlite'
SPEND_GAP_AMOUNT = '0.125'
SPEND_GAP_REVISED = '0.875'
SPEND_GAP_ROUNDING_AMOUNTS = ('0.0000000025', '-0.0000000035', None)
SPEND_GAP_DIMENSIONS = ('fixture-provider', 'fixture-day', 'fixture-intent', 'fixture-model')
SPEND_GAP_CALL_ID_PREFIX = 'nano-migration-call'
SPEND_GAP_MIGRATION_ROW_COUNT = ledger_read.DEFAULT_LEDGER_READ_ROW_CAP + len(SPEND_GAP_ROUNDING_AMOUNTS)


def deny_spend_gap_network(*args, **kwargs):
    raise OSError('nano schema migration guard is offline')


@pytest.mark.parametrize('prior_owner', (False, True))
def test_spend_gap_failed_managed_migration_restores_owner_and_retries(tmp_path, prior_owner):
    path, seed_connection = create_spend_gap_legacy_database(tmp_path)
    seed_connection.close()
    connection = config.open_ledger_connection(path)
    connection._ledger_owned_read_deadline = prior_owner
    try:
        with patch.object(ledger, 'usd_to_nano_usd', side_effect=ValueError('migration conversion failure')):
            with pytest.raises(ValueError, match='migration conversion failure'):
                calls._ensure_call_observation_schema(connection)
        assert connection._ledger_owned_read_deadline is prior_owner
        available = {row[1] for row in connection.execute('PRAGMA table_info(spend_events)')}
        assert not set(ledger.SPEND_EVENT_NANO_COLUMNS.values()) & available
        calls._ensure_call_observation_schema(connection)
        assert connection._ledger_owned_read_deadline is prior_owner
        for nano_column in ledger.SPEND_EVENT_NANO_COLUMNS.values():
            assert connection.execute(f'SELECT {nano_column} FROM spend_events').fetchone()[0] == usd_to_nano_usd(
                SPEND_GAP_AMOUNT)
    finally:
        connection.close()


@pytest.mark.parametrize('legacy_table', ('spend_events', 'calls'))
def test_spend_gap_pregate_populated_nano_is_preserved_while_nulls_repaired(tmp_path, legacy_table):
    path, connection = create_spend_gap_legacy_database(tmp_path)
    try:
        if legacy_table == 'calls':
            calls._initialize_call_observation_nano_schema(connection)
            connection.execute('DELETE FROM ledger_schema_migrations WHERE migration=?',
                               (calls.CALL_NANO_SCHEMA_MIGRATION,))
            columns = calls.CALL_OBSERVATION_NANO_COLUMNS
            connection.execute('INSERT INTO calls(id,cost,input_cost_floor) VALUES(?,?,?)',
                               (SPEND_GAP_CALL_ID_PREFIX, float(SPEND_GAP_AMOUNT), float(SPEND_GAP_AMOUNT)))
        else:
            columns = ledger.SPEND_EVENT_NANO_COLUMNS
            for nano_column in columns.values():
                connection.execute(f'ALTER TABLE spend_events ADD COLUMN {nano_column} INTEGER')
        populated_nano = next(iter(columns.values()))
        preserved = usd_to_nano_usd(SPEND_GAP_REVISED)
        connection.execute(f'UPDATE {legacy_table} SET {populated_nano}=?', (preserved,))
        calls._ensure_call_observation_schema(connection)
        for raw_column, nano_column in columns.items():
            raw, nano = connection.execute(f'SELECT {raw_column},{nano_column} FROM {legacy_table}').fetchone()
            converter = real_usd_to_nano_usd if legacy_table == 'calls' else usd_to_nano_usd
            assert nano == (preserved if nano_column == populated_nano else converter(raw))
    finally:
        connection.close()


@pytest.mark.parametrize('legacy_table', ('spend_events', 'calls'))
def test_spend_gap_managed_legacy_backfill_owns_reads_and_second_open_skips_scan(tmp_path, legacy_table):
    path, seed_connection = create_spend_gap_legacy_database(tmp_path)
    try:
        raw_spend_columns = tuple(ledger.SPEND_EVENT_NANO_COLUMNS)
        seed_connection.executemany(
            f"INSERT INTO spend_events({','.join(raw_spend_columns)}) "
            f"VALUES({','.join('?' for _ in raw_spend_columns)})",
            ((SPEND_GAP_AMOUNT,) * len(raw_spend_columns) for _ in range(SPEND_GAP_MIGRATION_ROW_COUNT)))
        if legacy_table == 'calls':
            seed_connection.execute('DROP TABLE spend_events')
            # Produce the complete legacy table from the existing schema initializer.
            calls._initialize_call_observation_nano_schema(seed_connection)
            if hasattr(calls, 'CALL_NANO_SCHEMA_MIGRATION'):
                seed_connection.execute('DELETE FROM ledger_schema_migrations WHERE migration=?',
                                        (calls.CALL_NANO_SCHEMA_MIGRATION,))
            for nano_column in calls.CALL_OBSERVATION_NANO_COLUMNS.values():
                seed_connection.execute(f'ALTER TABLE calls DROP COLUMN {nano_column}')
            raw_columns = tuple(calls.CALL_OBSERVATION_NANO_COLUMNS)
            rows = ((f'{SPEND_GAP_CALL_ID_PREFIX}-{index}', *((float(amount),) * len(raw_columns)))
                    for index, amount in enumerate((SPEND_GAP_AMOUNT, SPEND_GAP_REVISED)))
            columns = ('id', *raw_columns)
            seed_connection.executemany(
                f"INSERT INTO calls({','.join(columns)}) VALUES({','.join('?' for _ in columns)})", rows)
        seed_connection.commit()
    finally:
        seed_connection.close()
    with patch.object(socket.socket, 'connect', deny_spend_gap_network), \
            patch.object(socket.socket, 'connect_ex', deny_spend_gap_network), \
            patch.object(socket, 'create_connection', deny_spend_gap_network):
        connection = config.open_ledger_connection(path)
        try:
            assert not getattr(connection, '_ledger_owned_read_deadline', False)
            delayed_backfill_reads = []

            def delay_spend_gap_first_backfill_select(statement):
                normalized = ' '.join(statement.upper().split())
                if (not delayed_backfill_reads and normalized.startswith('SELECT ROWID,')
                        and f'FROM {legacy_table.upper()}' in normalized):
                    delayed_backfill_reads.append(statement)
                    # Real SQLite stepping crosses the unchanged normal read budget.
                    # The same slow migration read must succeed on its owned writer.
                    time.sleep(config.LEDGER_QUERY_DEADLINE_S + config.LEDGER_REQUEST_DISCONNECT_POLL_S)

            connection.set_trace_callback(delay_spend_gap_first_backfill_select)
            calls._ensure_call_observation_schema(connection)
            connection.set_trace_callback(None)
            assert delayed_backfill_reads
            assert not getattr(connection, '_ledger_owned_read_deadline', False)
            if legacy_table == 'spend_events':
                for raw_column, nano_column in ledger.SPEND_EVENT_NANO_COLUMNS.items():
                    rows = connection.execute(f'SELECT {raw_column},{nano_column} FROM spend_events').fetchall()
                    assert rows
                    assert all(nano == usd_to_nano_usd(raw) for raw, nano in rows)
            if legacy_table == 'calls':
                for raw_column, nano_column in calls.CALL_OBSERVATION_NANO_COLUMNS.items():
                    rows = connection.execute(f'SELECT {raw_column},{nano_column} FROM calls').fetchall()
                    assert rows
                    assert all(nano == real_usd_to_nano_usd(raw) for raw, nano in rows)
        finally:
            connection.close()
        reopened = config.open_ledger_connection(path)
        statements = []
        try:
            reopened.set_trace_callback(statements.append)
            with patch.object(calls, '_backfill_call_observation_nano_columns',
                              side_effect=AssertionError('completed calls migration scanned again')):
                calls._ensure_call_observation_schema(reopened)
            for statement in statements:
                normalized = ' '.join(statement.upper().split())
                if normalized.startswith('SELECT'):
                    assert 'FROM SPEND_EVENTS' not in normalized
                    assert 'FROM CALLS' not in normalized
        finally:
            reopened.close()


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
