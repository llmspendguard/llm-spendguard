"""Offline proof of measurements' additive nano mirror and unchanged REAL reader."""
import sqlite3
from decimal import Decimal, localcontext
from unittest.mock import patch

import pytest
from _pytest_fixture_bridge import align_test_startup_home, run_pytest_style

align_test_startup_home(prefix=__name__)

import spendguard
spendguard.require()
from spendguard import measurement
from spendguard.money_scale import real_usd_to_nano_usd, nano_usd_to_usd, assert_nano_total_matches

MEASUREMENT_NANO_PROOF_AMOUNTS = (-1.25, 0.0, None, 0.1234567895, 0.1, 0.2)
MEASUREMENT_NANO_PROOF_INVALID = (float('inf'), float('-inf'), 'NaN', 'malformed', 1e100)
MEASUREMENT_NANO_PROOF_LARGE = 5_000_000_000.0
MEASUREMENT_NANO_PROOF_PRECISION = 2
MEASUREMENT_NANO_PROOF_DB = 'measurements.sqlite'
MEASUREMENT_NANO_PROOF_KIND = 'nano-proof'


def create_measurement_nano_legacy_fixture(home, amounts=MEASUREMENT_NANO_PROOF_AMOUNTS):
    with patch.object(measurement.config, 'HOME', home), patch.object(
            measurement, '_ensure_measurement_nano_schema'):
        connection = measurement._measurements_db()
    for index, amount in enumerate(amounts):
        connection.execute('INSERT INTO measurements(reading_id,spend_usd) VALUES(?,?)',
                           (str(index), amount))
    connection.commit()
    return connection


def assert_measurement_nano_fixture_mirrors(connection):
    rows = connection.execute('SELECT spend_usd,spend_usd_nano FROM measurements').fetchall()
    for raw, nano in rows:
        assert nano == (None if raw is None else real_usd_to_nano_usd(raw))
    reference = sum((nano_usd_to_usd(real_usd_to_nano_usd(raw))
                     for raw, _ in rows if raw is not None), Decimal(0))
    assert_nano_total_matches(sum(nano for _, nano in rows if nano is not None),
                              reference, tolerance_usd='0', label='measurements.spend_usd')


def test_measurement_nano_backfill_raw_reader_and_idempotency(tmp_path):
    connection = create_measurement_nano_legacy_fixture(tmp_path)
    before = connection.execute('SELECT * FROM measurements').fetchall()
    expected_readings = [measurement._row_to_reading(row) for row in before]
    measurement._ensure_measurement_nano_schema(connection)
    assert_measurement_nano_fixture_mirrors(connection)
    assert connection.execute(measurement._SELECT).fetchall() == before
    with patch.object(measurement, '_backfill_measurement_nano_columns',
                      side_effect=AssertionError('backfill twice')):
        measurement._ensure_measurement_nano_schema(connection)
    with patch.object(measurement.config, 'HOME', tmp_path):
        for expected in expected_readings:
            assert measurement.get_reading(expected['reading_id']) == expected
        assert sorted(measurement.list_readings(), key=lambda reading: reading['reading_id']) == expected_readings
    connection.close()


@pytest.mark.parametrize('invalid_amount', MEASUREMENT_NANO_PROOF_INVALID)
def test_measurement_nano_invalid_rows_rollback_with_identity(tmp_path, invalid_amount):
    connection = create_measurement_nano_legacy_fixture(
        tmp_path, (*MEASUREMENT_NANO_PROOF_AMOUNTS, invalid_amount))
    before = connection.execute('SELECT * FROM measurements').fetchall()
    invalid_id = str(len(MEASUREMENT_NANO_PROOF_AMOUNTS))
    with pytest.raises(ValueError, match='measurement_nano_invalid_amount_gap count=1') as failure:
        measurement._ensure_measurement_nano_schema(connection)
    assert invalid_id in str(failure.value)
    assert 'spend_usd_nano' not in {row[1] for row in connection.execute('PRAGMA table_info(measurements)')}
    assert connection.execute('SELECT * FROM measurements').fetchall() == before
    connection.close()
    with patch.object(measurement.config, 'HOME', tmp_path), pytest.raises(ValueError):
        measurement._measurements_db()


@pytest.mark.parametrize('amount', MEASUREMENT_NANO_PROOF_AMOUNTS)
def test_measurement_nano_record_reading_derives_mirror(tmp_path, amount):
    arguments = dict(intent=__name__, kind=MEASUREMENT_NANO_PROOF_KIND, judge_mix=[],
                     sample_ids=[], rubric={}, candidates=[], values={}, spend_usd=amount)
    with patch.object(measurement.config, 'HOME', tmp_path):
        reading_id = measurement.record_reading(**arguments)
        with sqlite3.connect(tmp_path / MEASUREMENT_NANO_PROOF_DB) as connection:
            raw, nano = connection.execute(
                'SELECT spend_usd,spend_usd_nano FROM measurements WHERE reading_id=?', (reading_id,)).fetchone()
        assert raw == float(amount or 0.0)
        assert nano == real_usd_to_nano_usd(raw)
        assert measurement.get_reading(reading_id)['spend_usd'] == raw
        with pytest.raises(TypeError, match='spend_usd_nano'):
            measurement.record_reading(**arguments, spend_usd_nano=nano)


def test_measurement_nano_sqlite_nan_binding_becomes_legacy_null(tmp_path):
    connection = create_measurement_nano_legacy_fixture(tmp_path, (float('nan'),))
    assert connection.execute('SELECT spend_usd FROM measurements').fetchone()[0] is None
    measurement._ensure_measurement_nano_schema(connection)
    assert_measurement_nano_fixture_mirrors(connection)
    connection.close()


def test_measurement_nano_drift_rolls_back_preserving_outer_transaction(tmp_path):
    connection = create_measurement_nano_legacy_fixture(tmp_path)
    connection.execute('INSERT INTO measurements(reading_id,spend_usd) VALUES(?,?)',
                       (__name__, MEASUREMENT_NANO_PROOF_LARGE))
    with patch.object(measurement, 'assert_nano_total_matches', side_effect=AssertionError('injected drift')):
        with pytest.raises(AssertionError, match='injected drift'):
            measurement._ensure_measurement_nano_schema(connection)
    assert connection.in_transaction
    assert 'spend_usd_nano' not in {row[1] for row in connection.execute('PRAGMA table_info(measurements)')}
    measurement._ensure_measurement_nano_schema(connection)
    assert_measurement_nano_fixture_mirrors(connection)
    connection.close()


def test_measurement_nano_aggregate_avoids_sqlite_and_decimal_overflow(tmp_path):
    connection = create_measurement_nano_legacy_fixture(
        tmp_path, (MEASUREMENT_NANO_PROOF_LARGE, MEASUREMENT_NANO_PROOF_LARGE))
    with localcontext() as context:
        context.prec = MEASUREMENT_NANO_PROOF_PRECISION
        measurement._ensure_measurement_nano_schema(connection)
    assert_measurement_nano_fixture_mirrors(connection)
    with pytest.raises(sqlite3.OperationalError, match='integer overflow'):
        connection.execute('SELECT SUM(spend_usd_nano) FROM measurements').fetchone()
    connection.close()


if __name__ == '__main__':
    raise SystemExit(run_pytest_style(globals()))
