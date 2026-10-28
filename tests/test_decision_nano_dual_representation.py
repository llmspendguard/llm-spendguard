"""Offline proof of decisions REAL/nano storage across the FK rebuild."""
import sqlite3
from decimal import Decimal
from unittest.mock import patch

import pytest
from _pytest_fixture_bridge import align_test_startup_home, run_pytest_style

align_test_startup_home(prefix=__name__)

from spendguard import routing_audit, guard, config
from spendguard.money_scale import real_usd_to_nano_usd, nano_usd_to_usd, assert_nano_total_matches

DECISION_NANO_PROOF_AMOUNTS = (-1.25, 0.0, None, 0.1234567895, 0.1, 0.2)
DECISION_NANO_PROOF_INVALID = (float('inf'), float('-inf'), 'NaN', 'malformed', 1e100)
DECISION_NANO_PROOF_REVISED = 0.875
DECISION_NANO_PROOF_DB = 'decision-nano.sqlite'


def create_decision_nano_legacy_fixture(amounts=DECISION_NANO_PROOF_AMOUNTS, *, has_fk=False):
    connection = sqlite3.connect(':memory:')
    connection.execute('PRAGMA foreign_keys=ON')
    connection.execute('CREATE TABLE calls(id TEXT PRIMARY KEY)')
    routing_audit._create_decisions_table(connection, routing_audit.DECISIONS_TABLE)
    for column in routing_audit.DECISION_NANO_COLUMNS.values():
        connection.execute(f'ALTER TABLE decisions DROP COLUMN {column}')
    if not has_fk:
        connection.execute('CREATE TABLE legacy_decision_shape AS SELECT * FROM decisions')
        connection.execute('DROP TABLE decisions')
        connection.execute('ALTER TABLE legacy_decision_shape RENAME TO decisions')
    for amount in amounts:
        connection.execute('INSERT INTO decisions(counterfactual_usd,actual_usd,saved_usd) VALUES(?,?,?)',
                           (amount, amount, amount))
    connection.commit()
    return connection


def assert_decision_nano_fixture_mirrors(connection):
    for raw_column, nano_column in routing_audit.DECISION_NANO_COLUMNS.items():
        rows = connection.execute(f'SELECT {raw_column},{nano_column} FROM decisions').fetchall()
        for raw, nano in rows:
            assert nano == (None if raw is None else real_usd_to_nano_usd(raw))
        reference = sum((nano_usd_to_usd(real_usd_to_nano_usd(raw))
                         for raw, _ in rows if raw is not None), Decimal(0))
        assert_nano_total_matches(sum(nano for _, nano in rows if nano is not None), reference,
                                  tolerance_usd='0', label=raw_column)


@pytest.mark.parametrize('has_fk', (False, True))
def test_decision_nano_rebuild_raw_readers_and_idempotence(has_fk):
    connection = create_decision_nano_legacy_fixture(has_fk=has_fk)
    before = connection.execute('SELECT rowid,* FROM decisions').fetchall()
    sums = connection.execute('SELECT SUM(counterfactual_usd),SUM(actual_usd),SUM(saved_usd) FROM decisions').fetchone()
    routing_audit.ensure_decisions_call_fk_schema(connection)
    assert_decision_nano_fixture_mirrors(connection)
    assert connection.execute(f'SELECT rowid,{",".join(routing_audit.DECISION_COLUMNS)} FROM decisions').fetchall() == before
    assert connection.execute('SELECT SUM(counterfactual_usd),SUM(actual_usd),SUM(saved_usd) FROM decisions').fetchone() == sums
    assert connection.execute('PRAGMA foreign_key_list(decisions)').fetchall()
    with patch.object(routing_audit, '_backfill_decision_nano_columns', side_effect=AssertionError('backfill twice')):
        routing_audit.ensure_decisions_call_fk_schema(connection)
    assert_decision_nano_fixture_mirrors(connection)
    connection.close()


@pytest.mark.parametrize('invalid_amount', DECISION_NANO_PROOF_INVALID)
def test_decision_nano_invalid_rebuild_rolls_back(invalid_amount):
    connection = create_decision_nano_legacy_fixture((*DECISION_NANO_PROOF_AMOUNTS, invalid_amount))
    before = connection.execute('SELECT * FROM decisions').fetchall()
    with pytest.raises(ValueError, match='invalid_amount_gap.*count=1.*amount_count=3'):
        routing_audit.ensure_decisions_call_fk_schema(connection)
    assert connection.execute('SELECT * FROM decisions').fetchall() == before
    assert not set(routing_audit.DECISION_NANO_COLUMNS.values()).intersection(
        row[1] for row in connection.execute('PRAGMA table_info(decisions)'))
    assert not connection.execute('PRAGMA foreign_key_list(decisions)').fetchall()
    connection.close()


def test_decision_nano_fk_rebuild_carries_existing_mirrors():
    connection = create_decision_nano_legacy_fixture()
    routing_audit.ensure_decisions_call_fk_schema(connection)
    before = connection.execute('SELECT rowid,* FROM decisions').fetchall()
    connection.execute('CREATE TABLE existing_decision_mirrors AS SELECT rowid AS original_rowid,* FROM decisions')
    connection.execute('DROP TABLE decisions')
    connection.execute('ALTER TABLE existing_decision_mirrors RENAME TO decisions')
    connection.execute('ALTER TABLE decisions DROP COLUMN original_rowid')
    connection.commit()
    routing_audit.ensure_decisions_call_fk_schema(connection)
    assert connection.execute('SELECT rowid,* FROM decisions').fetchall() == before
    assert_decision_nano_fixture_mirrors(connection)
    connection.close()


def test_decision_nano_partial_schema_drift_rolls_back_ambient_transaction():
    connection = create_decision_nano_legacy_fixture()
    routing_audit.ensure_decisions_call_fk_schema(connection)
    connection.execute('ALTER TABLE decisions DROP COLUMN saved_usd_nano')
    connection.execute('UPDATE decisions SET actual_usd_nano=actual_usd_nano+1 WHERE actual_usd IS NOT NULL')
    with pytest.raises(AssertionError, match='tolerance USD 0'):
        routing_audit.ensure_decisions_call_fk_schema(connection, commit=False)
    assert connection.in_transaction
    assert 'saved_usd_nano' not in {row[1] for row in connection.execute('PRAGMA table_info(decisions)')}
    connection.close()


def test_decision_nano_linked_and_guard_writers_reject_direct_mirrors():
    connection = create_decision_nano_legacy_fixture()
    routing_audit.ensure_decisions_call_fk_schema(connection)
    writer_values = dict(call_id=None, project=__name__, intent=__name__, basis=__name__,
                         requested_model=None, requested_effort=None, chosen_model=None,
                         chosen_effort=None, counterfactual_usd=DECISION_NANO_PROOF_REVISED,
                         actual_usd=None, saved_usd=-DECISION_NANO_PROOF_REVISED,
                         why=__name__, reason_class=__name__)
    routing_audit.insert_linked_decision(connection, **writer_values)
    with patch.object(guard, '_decisions_db', return_value=connection):
        guard.record_decision.__wrapped__(intent=__name__, requested_model=None, chosen_model=None,
            counterfactual_usd=DECISION_NANO_PROOF_REVISED, actual_usd=None,
            saved_usd=-DECISION_NANO_PROOF_REVISED, project=__name__)
    assert connection.execute('SELECT COUNT(*) FROM decisions').fetchone()[0] == len(DECISION_NANO_PROOF_AMOUNTS) + 2
    assert_decision_nano_fixture_mirrors(connection)
    for column in routing_audit.DECISION_NANO_COLUMNS.values():
        with pytest.raises(TypeError, match=column):
            routing_audit.insert_linked_decision(connection, **writer_values, **{column: 0})
        with pytest.raises(TypeError, match=column):
            guard.record_decision.__wrapped__(intent=__name__, requested_model=None, chosen_model=None,
                counterfactual_usd=None, actual_usd=None, saved_usd=None, **{column: 0})
    connection.close()


def test_decision_nano_sqlite_nan_binding_is_null():
    connection = create_decision_nano_legacy_fixture((float('nan'),))
    assert connection.execute('SELECT actual_usd FROM decisions').fetchone()[0] is None
    routing_audit.ensure_decisions_call_fk_schema(connection)
    assert_decision_nano_fixture_mirrors(connection)
    connection.close()


def test_decision_nano_real_readers_preserve_values(tmp_path):
    connection = create_decision_nano_legacy_fixture()
    path = tmp_path / DECISION_NANO_PROOF_DB
    database = sqlite3.connect(path)
    connection.backup(database)
    connection.close()
    with patch.object(config, 'db_path', return_value=path):
        before_rows = guard.decisions_since()
        before_summary = guard.decisions_summary()
        routing_audit.ensure_decisions_call_fk_schema(database)
        assert guard.decisions_since() == before_rows
        assert guard.decisions_summary() == before_summary
        assert not set(routing_audit.DECISION_NANO_COLUMNS.values()).intersection(before_rows[0])
    database.close()


if __name__ == '__main__':
    raise SystemExit(run_pytest_style(globals()))
