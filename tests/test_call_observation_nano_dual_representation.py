"""Offline proof of additive REAL/nano calls storage; readers retain REAL sums."""
import sqlite3
import runpy
from pathlib import Path
from decimal import Decimal
from unittest.mock import patch

import pytest
from _pytest_fixture_bridge import align_test_startup_home, run_pytest_style

align_test_startup_home(prefix=__name__)

from spendguard import calls, ledger_read
from spendguard.ledger_schema_migrations import CALL_NANO_SCHEMA_MIGRATION
from spendguard.money_scale import real_usd_to_nano_usd, nano_usd_to_usd, assert_nano_total_matches

CALL_NANO_PROOF_AMOUNTS = (-1.25, 0.0, None, 0.1234567895, 0.1, 0.2)
CALL_NANO_PROOF_INVALID = (float('inf'), float('-inf'), 'NaN', 'malformed', 1e100)
CALL_NANO_PROOF_DB = 'call-observation-nano.sqlite'
CALL_NANO_PROOF_PROVIDER = 'openai'
CALL_NANO_PROOF_ALIAS_PROVIDER = 'anthropic'
CALL_NANO_PROOF_MODEL = 'gpt-5-nano'
CALL_NANO_PROOF_DATED_ALIAS = 'claude-haiku-4-5-20251001'
CALL_NANO_PROOF_REVISED = 0.875
CALL_NANO_PROOF_FLOOR = 0.025
CALL_NANO_PROOF_LARGE_AMOUNT = 5_000_000_000.0
CALL_NANO_PROOF_MAINTENANCE_SCRIPT = Path(__file__).resolve().parents[1] / 'scripts' / 'maintenance' / 'reattribute_ledger_provider.py'


def create_call_nano_legacy_fixture(path, amounts=CALL_NANO_PROOF_AMOUNTS):
    connection = sqlite3.connect(path)
    # Create the full current legacy shape without nano migration; fixture only.
    with patch.object(calls, '_backfill_call_observation_nano_columns'):
        calls._ensure_call_observation_schema(connection)
    for column in calls.CALL_OBSERVATION_NANO_COLUMNS.values():
        connection.execute(f'ALTER TABLE calls DROP COLUMN {column}')
    connection.execute('DELETE FROM ledger_schema_migrations WHERE migration=?', (CALL_NANO_SCHEMA_MIGRATION,))
    for index, amount in enumerate(amounts):
        connection.execute('INSERT INTO calls(id,cost,input_cost_floor) VALUES(?,?,?)',
                           (str(index), amount, amount))
    connection.commit()
    return connection


def assert_call_nano_fixture_mirrors(connection):
    for raw_column, nano_column in calls.CALL_OBSERVATION_NANO_COLUMNS.items():
        rows = connection.execute(f'SELECT {raw_column},{nano_column} FROM calls').fetchall()
        for raw, nano in rows:
            assert nano == (None if raw is None else real_usd_to_nano_usd(raw))
        reference = sum((nano_usd_to_usd(real_usd_to_nano_usd(raw))
                         for raw, _ in rows if raw is not None), Decimal(0))
        total = sum(nano for _, nano in rows if nano is not None)
        assert_nano_total_matches(total, reference, tolerance_usd='0', label=raw_column)


def test_call_nano_migration_preserves_raw_readers_and_is_idempotent(tmp_path):
    path = tmp_path / CALL_NANO_PROOF_DB
    connection = create_call_nano_legacy_fixture(path)
    before = connection.execute('SELECT id,cost,input_cost_floor FROM calls').fetchall()
    # FLAGGED EXPECTED: the sub-nano fixture amount is intentionally quantized through the money SSOT.
    expected = float(nano_usd_to_usd(sum(
        real_usd_to_nano_usd(raw) for raw in CALL_NANO_PROOF_AMOUNTS if raw is not None)))
    calls._ensure_call_observation_schema(connection)
    assert_call_nano_fixture_mirrors(connection)
    assert connection.execute('SELECT id,cost,input_cost_floor FROM calls').fetchall() == before
    assert ledger_read.read_recorded_call_total(path) == expected
    with patch.object(calls, '_backfill_call_observation_nano_columns', side_effect=AssertionError('backfill twice')):
        calls._ensure_call_observation_schema(connection)
    assert_call_nano_fixture_mirrors(connection)
    connection.close()


@pytest.mark.parametrize('invalid_amount', CALL_NANO_PROOF_INVALID)
def test_call_nano_invalid_rows_roll_back_entire_migration(tmp_path, invalid_amount):
    connection = create_call_nano_legacy_fixture(tmp_path / CALL_NANO_PROOF_DB,
                                                 (*CALL_NANO_PROOF_AMOUNTS, invalid_amount))
    before = connection.execute('SELECT * FROM calls').fetchall()
    with pytest.raises(ValueError, match='invalid_amount_gap.*count=1.*amount_count=2'):
        calls._ensure_call_observation_schema(connection)
    assert not set(calls.CALL_OBSERVATION_NANO_COLUMNS.values()).intersection(
        row[1] for row in connection.execute('PRAGMA table_info(calls)'))
    assert connection.execute('SELECT * FROM calls').fetchall() == before
    connection.close()


def test_call_nano_normal_writers_update_and_reject_derived_fields(tmp_path):
    connection = create_call_nano_legacy_fixture(tmp_path / CALL_NANO_PROOF_DB)
    calls._ensure_call_observation_schema(connection)
    with patch.object(calls, '_calls_db', return_value=connection), patch.object(
            calls, '_out_tok_sanity_ceiling', return_value=(None, None)):
        recorded = calls.record_call.__wrapped__(CALL_NANO_PROOF_PROVIDER, CALL_NANO_PROOF_MODEL,
            'batch', CALL_NANO_PROOF_FLOOR, in_tok=0, out_tok=0, intent=__name__, project=__name__)
        calls.insert.__wrapped__(CALL_NANO_PROOF_PROVIDER, CALL_NANO_PROOF_MODEL, 'realtime',
            CALL_NANO_PROOF_REVISED, intent=__name__, project=__name__)
        calls.insert.__wrapped__(CALL_NANO_PROOF_ALIAS_PROVIDER, CALL_NANO_PROOF_DATED_ALIAS, 'realtime',
            CALL_NANO_PROOF_REVISED, intent=__name__, project=__name__)
        calls.record_call.__wrapped__(CALL_NANO_PROOF_PROVIDER, CALL_NANO_PROOF_MODEL, 'realtime',
            None, in_tok=None, out_tok=None, input_cost_floor=CALL_NANO_PROOF_FLOOR,
            intent=__name__, project=__name__)
        assert calls.reconcile_calls_batch_estimate.__wrapped__(CALL_NANO_PROOF_PROVIDER,
            CALL_NANO_PROOF_MODEL, __name__, 0, 0, CALL_NANO_PROOF_REVISED)
        assert connection.execute('SELECT cost FROM calls WHERE id=?', (recorded,)).fetchone()[0] == CALL_NANO_PROOF_REVISED
        assert_call_nano_fixture_mirrors(connection)
        assert connection.execute(
            'SELECT COUNT(*) FROM calls WHERE cost IS NOT NULL AND cost_nano IS NULL').fetchone()[0] == 0
        for derived_column in calls.CALL_OBSERVATION_NANO_COLUMNS.values():
            with pytest.raises(TypeError, match=derived_column):
                calls.insert.__wrapped__(CALL_NANO_PROOF_PROVIDER, CALL_NANO_PROOF_MODEL, 'realtime',
                                         CALL_NANO_PROOF_FLOOR, **{derived_column: 0})
    connection.close()


def test_call_nano_sqlite_nan_binding_is_null(tmp_path):
    connection = create_call_nano_legacy_fixture(tmp_path / CALL_NANO_PROOF_DB, (float('nan'),))
    assert connection.execute('SELECT cost FROM calls').fetchone()[0] is None
    calls._ensure_call_observation_schema(connection)
    assert_call_nano_fixture_mirrors(connection)
    connection.close()


def test_call_nano_drift_failure_preserves_ambient_transaction(tmp_path):
    connection = create_call_nano_legacy_fixture(tmp_path / CALL_NANO_PROOF_DB)
    connection.execute('INSERT INTO calls(cost) VALUES(?)', (CALL_NANO_PROOF_REVISED,))
    with patch.object(calls, 'assert_nano_total_matches', side_effect=AssertionError('injected drift')):
        with pytest.raises(AssertionError, match='injected drift'):
            calls._ensure_call_observation_schema(connection)
    assert connection.in_transaction
    assert not set(calls.CALL_OBSERVATION_NANO_COLUMNS.values()).intersection(
        row[1] for row in connection.execute('PRAGMA table_info(calls)'))
    calls._ensure_call_observation_schema(connection)
    assert_call_nano_fixture_mirrors(connection)
    connection.close()


def test_call_nano_maintenance_writer_dual_writes(tmp_path):
    path = tmp_path / CALL_NANO_PROOF_DB
    connection = create_call_nano_legacy_fixture(path)
    connection.row_factory = sqlite3.Row
    rows = connection.execute('SELECT id,in_tok,out_tok,cost FROM calls').fetchall()
    connection.close()
    namespace = runpy.run_path(str(CALL_NANO_PROOF_MAINTENANCE_SCRIPT))
    with patch.object(namespace['config'], 'db_path', return_value=path), patch.object(
            ledger_read, 'read_ledger_sql', return_value=rows), patch.object(
            namespace['pricing'], 'realtime_cost', return_value=CALL_NANO_PROOF_REVISED), patch.object(
            namespace['pricing'], 'price', return_value={'in_': None, 'out': None}):
        assert namespace['main'](['--model', CALL_NANO_PROOF_MODEL, '--from-provider',
                                  CALL_NANO_PROOF_PROVIDER, '--to-provider', __name__, '--apply']) == 0
    with sqlite3.connect(path) as connection:
        assert_call_nano_fixture_mirrors(connection)
        assert all(row[0] == CALL_NANO_PROOF_REVISED for row in connection.execute('SELECT cost FROM calls'))


def test_call_nano_partial_schema_preserves_populated_mirrors(tmp_path):
    connection = create_call_nano_legacy_fixture(tmp_path / CALL_NANO_PROOF_DB)
    calls._ensure_call_observation_schema(connection)
    connection.execute('ALTER TABLE calls DROP COLUMN input_cost_floor_nano')
    connection.execute('UPDATE calls SET cost_nano=cost_nano+1 WHERE cost IS NOT NULL')
    connection.execute('DELETE FROM ledger_schema_migrations WHERE migration=?', (CALL_NANO_SCHEMA_MIGRATION,))
    preserved_cost_nano = connection.execute('SELECT id,cost_nano FROM calls ORDER BY id').fetchall()
    connection.commit()
    calls._ensure_call_observation_schema(connection)
    assert connection.execute('SELECT id,cost_nano FROM calls ORDER BY id').fetchall() == preserved_cost_nano
    for raw_floor, nano_floor in connection.execute('SELECT input_cost_floor,input_cost_floor_nano FROM calls'):
        assert nano_floor == (None if raw_floor is None else real_usd_to_nano_usd(raw_floor))
    connection.close()


def test_call_nano_aggregate_beyond_sqlite_integer_is_exact(tmp_path):
    connection = create_call_nano_legacy_fixture(tmp_path / CALL_NANO_PROOF_DB,
        (CALL_NANO_PROOF_LARGE_AMOUNT, CALL_NANO_PROOF_LARGE_AMOUNT))
    calls._ensure_call_observation_schema(connection)
    assert_call_nano_fixture_mirrors(connection)
    with pytest.raises(sqlite3.OperationalError, match='integer overflow'):
        connection.execute('SELECT SUM(cost_nano) FROM calls').fetchone()
    connection.close()


if __name__ == '__main__':
    raise SystemExit(run_pytest_style(globals()))
