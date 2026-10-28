"""Offline Stage 4 auxiliary REAL mirrors; run as a gated script."""
import spendguard
spendguard.require()

import sqlite3
import unittest
import contextlib
from types import SimpleNamespace
from decimal import Decimal
from unittest.mock import patch

from spendguard import guard, bulkgate, calibrate, effort_titration
from spendguard.money_scale import real_usd_to_nano_usd, nano_usd_to_usd

AUXILIARY_NANO_CASES = (
    ("savings", guard._ensure_guard_savings_schema, guard.SAVINGS_NANO_COLUMNS),
    ("gate_ledger", bulkgate._ensure_gate_schema, bulkgate.GATE_ESTIMATE_NANO_COLUMNS),
    ("cost_predictions", calibrate._ensure_cost_predictions_schema, calibrate.COST_PREDICTION_NANO_COLUMNS),
    ("effort_titration", effort_titration._ensure_effort_progress_schema, effort_titration.EFFORT_PROGRESS_NANO_COLUMNS),
)
AUXILIARY_NANO_AMOUNTS = (-0.123456789, 0.0, None, 0.0000000005, 0.3)
AUXILIARY_NANO_INVALID_AMOUNTS = (float("inf"), float("-inf"), "NaN", 1e100)
AUXILIARY_NANO_DATABASE = ":memory:"
AUXILIARY_NANO_ENTITY = "fixture"
AUXILIARY_NANO_SOURCE = "cache"
AUXILIARY_NANO_WRITER_AMOUNTS = (0.3, 0.6)


def auxiliary_nano_legacy_fixture(table, initializer, columns):
    database = sqlite3.connect(AUXILIARY_NANO_DATABASE)
    initializer(database)
    declarations = [f'"{row[1]}" {row[2]}' for row in database.execute(f"PRAGMA table_info({table})")
                    if row[1] not in columns.values()]
    database.execute(f"DROP TABLE {table}")
    database.execute(f"CREATE TABLE {table} ({','.join(declarations)})")
    database.commit()
    return database


class AuxiliaryNanoDualwriteChecks(unittest.TestCase):
    def test_auxiliary_nano_backfill_exact_idempotent_raw_preserved(self):
        for table, initializer, columns in AUXILIARY_NANO_CASES:
            with self.subTest(table=table):
                database = auxiliary_nano_legacy_fixture(table, initializer, columns)
                raw_columns = ','.join(columns)
                placeholders = ','.join('?' for _ in columns)
                for amount in AUXILIARY_NANO_AMOUNTS:
                    database.execute(f"INSERT INTO {table} ({raw_columns}) VALUES ({placeholders})",
                                     tuple(amount for _ in columns))
                database.commit()
                before = database.execute(f"SELECT {raw_columns} FROM {table}").fetchall()
                initializer(database)
                initializer(database)
                self.assertEqual(before, database.execute(f"SELECT {raw_columns} FROM {table}").fetchall())
                for raw, nano in columns.items():
                    pairs = database.execute(f"SELECT {raw},{nano} FROM {table}").fetchall()
                    for value, mirror in pairs:
                        self.assertEqual(mirror, None if value is None else real_usd_to_nano_usd(value))
                    self.assertEqual(nano_usd_to_usd(sum(n for _, n in pairs if n is not None)),
                                     sum((nano_usd_to_usd(real_usd_to_nano_usd(v)) for v, _ in pairs if v is not None), Decimal(0)))
                database.close()

    def test_auxiliary_nano_invalid_rows_rollback_schema_and_data(self):
        for table, initializer, columns in AUXILIARY_NANO_CASES:
            for invalid in AUXILIARY_NANO_INVALID_AMOUNTS:
                with self.subTest(table=table, invalid=invalid):
                    database = auxiliary_nano_legacy_fixture(table, initializer, columns)
                    raw = next(iter(columns))
                    database.execute(f"INSERT INTO {table} ({raw}) VALUES (?)", (invalid,))
                    database.commit()
                    before = database.execute(f"SELECT * FROM {table}").fetchall()
                    with self.assertRaisesRegex(ValueError, "invalid_amount_gap count=1.*rows="):
                        initializer(database)
                    self.assertTrue(set(columns.values()).isdisjoint(row[1] for row in database.execute(f"PRAGMA table_info({table})")))
                    self.assertEqual(before, database.execute(f"SELECT * FROM {table}").fetchall())
                    database.close()

    def test_auxiliary_nano_backfill_once_and_drift_rollback(self):
        for table, initializer, columns in AUXILIARY_NANO_CASES:
            with self.subTest(table=table):
                database = auxiliary_nano_legacy_fixture(table, initializer, columns)
                raw = next(iter(columns))
                database.execute(f"INSERT INTO {table} ({raw}) VALUES (?)", (AUXILIARY_NANO_WRITER_AMOUNTS[0],))
                database.commit()
                owner = __import__(initializer.__module__, fromlist=['unused'])
                database.execute("BEGIN")
                with patch.object(owner, 'assert_nano_total_matches', side_effect=AssertionError('injected drift')):
                    with self.assertRaisesRegex(AssertionError, 'injected drift'):
                        initializer(database)
                self.assertTrue(database.in_transaction)
                self.assertTrue(set(columns.values()).isdisjoint(row[1] for row in database.execute(f"PRAGMA table_info({table})")))
                database.rollback()
                initializer(database)
                with patch.object(owner, 'real_usd_to_nano_usd', side_effect=AssertionError('backfilled twice')):
                    initializer(database)
                database.close()

    def test_auxiliary_nano_savings_and_gate_writers(self):
        database = sqlite3.connect(AUXILIARY_NANO_DATABASE)
        guard._ensure_guard_savings_schema(database)
        bulkgate._ensure_gate_schema(database)
        with patch.object(guard, '_savings_db', return_value=database), patch.object(bulkgate, '_gate_db', return_value=database), patch.object(calibrate, 'record_prediction'):
            guard.record_saving.__wrapped__(AUXILIARY_NANO_SOURCE, 0.123456789, project=AUXILIARY_NANO_ENTITY)
            for amount in AUXILIARY_NANO_WRITER_AMOUNTS:
                bulkgate.record_estimate.__wrapped__(AUXILIARY_NANO_ENTITY, AUXILIARY_NANO_ENTITY, amount, 1)
                self.assertEqual(database.execute("SELECT est_usd_nano FROM gate_ledger").fetchone()[0], real_usd_to_nano_usd(amount))
        raw, nano = database.execute("SELECT amount,amount_nano FROM savings").fetchone()
        self.assertEqual(nano, real_usd_to_nano_usd(raw))

    def test_auxiliary_nano_prediction_and_effort_replace(self):
        database = sqlite3.connect(AUXILIARY_NANO_DATABASE)
        calibrate._ensure_cost_predictions_schema(database)
        effort_titration._ensure_effort_progress_schema(database)
        with patch.object(calibrate, '_con', return_value=database), patch.object(calibrate, '_identity', return_value=(None, None)), patch.object(effort_titration, '_ckpt_db', return_value=database):
            for amount in (0.3, -0.6, None):
                calibrate.record_prediction.__wrapped__(AUXILIARY_NANO_ENTITY, AUXILIARY_NANO_ENTITY, AUXILIARY_NANO_ENTITY, amount)
                self.assertEqual(database.execute("SELECT predicted_usd_nano,actual_usd_nano FROM cost_predictions").fetchone(),
                                 (None if amount is None else real_usd_to_nano_usd(amount), None))
                accumulator = effort_titration._new_acc()
                accumulator['cost'] = amount
                effort_titration._save_one.__wrapped__(AUXILIARY_NANO_ENTITY, AUXILIARY_NANO_ENTITY, AUXILIARY_NANO_ENTITY, accumulator)
                self.assertEqual(database.execute("SELECT cost_nano FROM effort_titration").fetchone()[0],
                                 None if amount is None else real_usd_to_nano_usd(amount))
            accumulator['cost_nano'] = 0
            with self.assertRaisesRegex(TypeError, 'derived'):
                effort_titration._save_one.__wrapped__(AUXILIARY_NANO_ENTITY, AUXILIARY_NANO_ENTITY, AUXILIARY_NANO_ENTITY, accumulator)

    def test_auxiliary_nano_pair_and_real_readers(self):
        from spendguard import ledger_read
        database = sqlite3.connect(AUXILIARY_NANO_DATABASE)
        calibrate._ensure_cost_predictions_schema(database)
        effort_titration._ensure_effort_progress_schema(database)
        actual_rows = [SimpleNamespace(values=(10, 20, 0.1234567, 'batch', AUXILIARY_NANO_ENTITY))]
        with patch.object(calibrate, '_con', return_value=database), patch.object(calibrate, '_identity', return_value=(None, None)), patch.object(calibrate, 'current_ledger_writer_connection', return_value=database), patch.object(ledger_read, 'read_calibration_actuals', return_value=actual_rows):
            calibrate.record_prediction.__wrapped__(AUXILIARY_NANO_ENTITY, AUXILIARY_NANO_ENTITY, AUXILIARY_NANO_ENTITY, 0.4)
            self.assertEqual(calibrate.pair.__wrapped__()['paired'], 1)
        raw, nano = database.execute("SELECT actual_usd,actual_usd_nano FROM cost_predictions").fetchone()
        self.assertEqual(raw, 0.123457)
        self.assertEqual(nano, real_usd_to_nano_usd(raw))
        with patch.object(effort_titration, '_ckpt_db', return_value=database):
            accumulator = effort_titration._new_acc()
            accumulator['cost'] = 0.1234567891
            effort_titration._save_one.__wrapped__(AUXILIARY_NANO_ENTITY, AUXILIARY_NANO_ENTITY, AUXILIARY_NANO_ENTITY, accumulator)
        with patch.object(effort_titration.config, 'ledger_op', return_value=contextlib.nullcontext(database)):
            self.assertEqual(effort_titration._load_progress(AUXILIARY_NANO_ENTITY, AUXILIARY_NANO_ENTITY, [AUXILIARY_NANO_ENTITY])[AUXILIARY_NANO_ENTITY]['cost'], accumulator['cost'])
        # Reader sees preserved REAL even where sub-nano precision differs from mirror.
        guard._ensure_guard_savings_schema(database)
        with patch.object(guard, '_savings_db', return_value=database):
            guard.record_saving.__wrapped__(AUXILIARY_NANO_SOURCE, 0.1234567891, project=AUXILIARY_NANO_ENTITY)
        source_rows = [{'source': source, 'amount': amount} for source, amount in database.execute("SELECT source,SUM(amount) FROM savings GROUP BY source")]
        with patch.object(guard.Path, 'exists', return_value=True), patch.object(ledger_read, 'ledger_read_table_exists', return_value=True), patch.object(ledger_read, 'read_ledger_sql', return_value=source_rows):
            self.assertEqual(guard.saved_since()['by_source'][AUXILIARY_NANO_SOURCE], round(0.1234567891, 4))


if __name__ == '__main__':
    unittest.main()
