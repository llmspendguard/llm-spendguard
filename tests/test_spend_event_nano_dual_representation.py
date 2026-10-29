"""Offline arithmetic and rollback proof for the additive spend_events nano shadow."""

import logging
import sqlite3
from decimal import Decimal
from unittest.mock import patch

import pytest

from _pytest_fixture_bridge import align_test_startup_home, run_pytest_style

align_test_startup_home(prefix=__name__)

from spendguard import ledger as nano_ledger_module
from spendguard import budget as nano_budget_module
from spendguard.money_scale import assert_nano_total_matches, usd_to_nano_usd, nano_usd_to_usd


NANO_FIXTURE_DB_NAME = "nano-dual-representation.sqlite"
NANO_FIXTURE_STATUSES = ("draft", "posted", "reconciled", "locked", "reversed", "void")
NANO_FIXTURE_AMOUNTS = ("1.230000000", "-0.000000017", "0", None, "3.1415926535", "-5.75")
NANO_FIXTURE_BAD_AMOUNTS = ("malformed", "NaN", "Infinity", "1e1000")
NANO_FIXTURE_ZERO = "0"
NANO_FIXTURE_PRIMARY_AMOUNT = "2.500000001"
NANO_FIXTURE_REVISED_AMOUNT = "1.000000001"
NANO_FIXTURE_CURRENCY = "USD"
NANO_FIXTURE_OTHER_CURRENCY = "EUR"
NANO_FIXTURE_DRIFT_USD = "0.02"
NANO_FIXTURE_MEMORY_DATABASE = ":memory:"
NANO_FIXTURE_AGGREGATE_OVERFLOW_AMOUNT = "5000000000"
NANO_FIXTURE_AGGREGATE_OVERFLOW_ROWS = 2
NANO_FIXTURE_EXCLUDED_FLAGS = (("draft", "is_meta", 1), ("posted", "reconciled", 1),
                             ("reconciled", "cost_basis", "reconstructed"))


def create_legacy_nano_proof_database(directory, *, bad_amount=None, other_currency=False):
    database_path = directory / NANO_FIXTURE_DB_NAME
    with sqlite3.connect(database_path) as connection:
        connection.create_aggregate("dec_sum", 1, nano_ledger_module._DecSum)
        connection.execute(nano_ledger_module._DDL_EVENTS)
        legacy_columns = {row[1] for row in connection.execute("PRAGMA table_info(spend_events)")}
        assert not legacy_columns.intersection(nano_ledger_module.SPEND_EVENT_NANO_COLUMNS.values())
        money_columns = tuple(nano_ledger_module.SPEND_EVENT_NANO_COLUMNS)
        columns = ("id", "source", "currency", "status", *money_columns)
        statement = (f"INSERT INTO spend_events ({','.join(columns)}) VALUES "
                     f"({','.join('?' for _ in columns)})")
        for row_index, status in enumerate(NANO_FIXTURE_STATUSES):
            amounts = tuple(NANO_FIXTURE_AMOUNTS[(row_index + column_index) % len(NANO_FIXTURE_AMOUNTS)]
                            for column_index, _ in enumerate(money_columns))
            connection.execute(statement, (f"{__name__}:{status}", __name__, NANO_FIXTURE_CURRENCY,
                                           status, *amounts))
        for status, flag, value in NANO_FIXTURE_EXCLUDED_FLAGS:
            connection.execute(f"UPDATE spend_events SET {flag}=? WHERE status=?", (value, status))
        if bad_amount is not None or other_currency:
            edge_amount = bad_amount if bad_amount is not None else NANO_FIXTURE_PRIMARY_AMOUNT
            connection.execute(statement, (f"{__name__}:edge", __name__,
                                           NANO_FIXTURE_OTHER_CURRENCY if other_currency else NANO_FIXTURE_CURRENCY,
                                           "posted", edge_amount, *([None] * (len(money_columns) - 1))))
        before_rows = connection.execute("SELECT * FROM spend_events ORDER BY id").fetchall()
        before_total = tuple(connection.execute(
            f"SELECT dec_sum({column}) FROM spend_events WHERE {nano_ledger_module.SpendLedger._COUNTABLE}").fetchone()[0]
            for column in nano_ledger_module.USD_COLS)
    return database_path, before_rows, before_total


def assert_spend_event_shadow_rows(connection):
    connection.row_factory = sqlite3.Row
    for event in connection.execute("SELECT * FROM spend_events"):
        for text_column, nano_column in nano_ledger_module.SPEND_EVENT_NANO_COLUMNS.items():
            raw = event[text_column]
            expected = (None if raw is None or event['currency'] != NANO_FIXTURE_CURRENCY
                        else usd_to_nano_usd(raw))
            assert event[nano_column] == expected, (event['id'], text_column, raw, event[nano_column])


def test_spend_event_nano_backfill_read_preservation_and_idempotency(tmp_path):
    database_path, before_rows, before_total = create_legacy_nano_proof_database(tmp_path)
    migrated = nano_ledger_module.SpendLedger(database_path)
    expected_spent = sum((nano_usd_to_usd(usd_to_nano_usd(Decimal(value or NANO_FIXTURE_ZERO)))
                          for value in before_total[:2]), Decimal(0))
    # spend_events readers are nano-granular post-cutover; raw TEXT retains full precision.
    assert Decimal(migrated.spent_dec()) == nano_usd_to_usd(usd_to_nano_usd(expected_spent))
    with patch.object(nano_budget_module, '_ledger', return_value=migrated):
        assert nano_budget_module.spent_all_time() == float(expected_spent)
    with sqlite3.connect(database_path) as source_connection, sqlite3.connect(NANO_FIXTURE_MEMORY_DATABASE) as owned_connection:
        source_connection.backup(owned_connection)
        owned_ledger = nano_ledger_module.SpendLedger(NANO_FIXTURE_MEMORY_DATABASE, owned_connection=owned_connection)
        for column, before_value in zip(nano_ledger_module.USD_COLS, before_total):
            assert Decimal(owned_ledger._cat_dec((column,), migrated._COUNTABLE)) == nano_usd_to_usd(
                usd_to_nano_usd(Decimal(before_value or NANO_FIXTURE_ZERO)))
    with sqlite3.connect(database_path) as connection:
        assert_spend_event_shadow_rows(connection)
        old_column_count = len(before_rows[0])
        assert [tuple(row)[:old_column_count] for row in connection.execute(
            "SELECT * FROM spend_events ORDER BY id")] == before_rows
        connection.create_aggregate("dec_sum", 1, nano_ledger_module._DecSum)
        for filter_sql in ("1=1", migrated._COUNTABLE):
            category_nanos = 0
            category_text = Decimal(0)
            for text_column, nano_column in nano_ledger_module.SPEND_EVENT_NANO_COLUMNS.items():
                nano_total, text_total = connection.execute(
                    f"SELECT SUM({nano_column}), dec_sum({text_column}) FROM spend_events WHERE {filter_sql}").fetchone()
                reference = Decimal(text_total or NANO_FIXTURE_ZERO)
                assert_nano_total_matches(nano_total or 0, reference, label=text_column)
                if text_column in nano_ledger_module.USD_COLS:
                    category_nanos += nano_total or 0
                    category_text += reference
            assert_nano_total_matches(category_nanos, category_text, label=__name__)
        unchanged_rows = connection.execute("SELECT * FROM spend_events ORDER BY id").fetchall()
    with patch.object(nano_ledger_module.SpendLedger, '_backfill_spend_event_nano_columns',
                      side_effect=AssertionError("second open must not backfill")):
        nano_ledger_module.SpendLedger(database_path)
    with sqlite3.connect(database_path) as connection:
        assert connection.execute("SELECT * FROM spend_events ORDER BY id").fetchall() == [tuple(row) for row in unchanged_rows]


@pytest.mark.parametrize("bad_amount", NANO_FIXTURE_BAD_AMOUNTS)
def test_spend_event_nano_invalid_amount_rolls_back_schema_and_rows(tmp_path, bad_amount):
    database_path, before_rows, _ = create_legacy_nano_proof_database(tmp_path, bad_amount=bad_amount)
    with pytest.raises(ValueError, match="spend_event_nano_invalid_amount_gap.*count=1.*edge"):
        nano_ledger_module.SpendLedger(database_path)
    with sqlite3.connect(database_path) as connection:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(spend_events)")}
        assert not columns.intersection(nano_ledger_module.SPEND_EVENT_NANO_COLUMNS.values())
        assert connection.execute("SELECT * FROM spend_events ORDER BY id").fetchall() == before_rows


def test_spend_event_nano_non_usd_named_gap(tmp_path):
    database_path, _, _ = create_legacy_nano_proof_database(tmp_path, other_currency=True)
    with patch.object(logging.getLogger(nano_ledger_module.__name__), "warning") as warning:
        nano_ledger_module.SpendLedger(database_path)
    assert warning.call_count == 1
    assert warning.call_args.args[0].startswith("spend_event_nano_non_usd_gap")
    assert warning.call_args.args[1:] == (1, [f"{__name__}:edge"])
    with sqlite3.connect(database_path) as connection:
        assert_spend_event_shadow_rows(connection)


def test_spend_event_nano_drift_guard_rejects_and_rolls_back(tmp_path):
    database_path, before_rows, _ = create_legacy_nano_proof_database(tmp_path)
    drift_nanos = usd_to_nano_usd(NANO_FIXTURE_DRIFT_USD)
    with patch.object(nano_ledger_module, 'usd_to_nano_usd',
                      side_effect=lambda raw: usd_to_nano_usd(raw) + drift_nanos):
        with pytest.raises(AssertionError, match="differs from reference"):
            nano_ledger_module.SpendLedger(database_path)
    with sqlite3.connect(database_path) as connection:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(spend_events)")}
        assert not columns.intersection(nano_ledger_module.SPEND_EVENT_NANO_COLUMNS.values())
        assert connection.execute("SELECT * FROM spend_events ORDER BY id").fetchall() == before_rows


def test_spend_event_nano_insert_update_and_correction_writers(tmp_path):
    database_path = tmp_path / NANO_FIXTURE_DB_NAME
    ledger = nano_ledger_module.SpendLedger(database_path)
    event = {"source": __name__, "dedup_key": f"{__name__}:insert", "batch_usd": NANO_FIXTURE_PRIMARY_AMOUNT,
             "base_usd": NANO_FIXTURE_PRIMARY_AMOUNT, "status": "posted"}
    event[nano_ledger_module.SPEND_EVENT_NANO_COLUMNS['batch_usd']] = 0
    event_id = ledger.record_event(event)
    ledger.update(event_id, {"batch_usd": NANO_FIXTURE_REVISED_AMOUNT, "base_usd": None})
    correction_id = ledger.adjust(event_id, {"batch_usd": NANO_FIXTURE_PRIMARY_AMOUNT})
    reversal_id = ledger.reverse(correction_id)
    assert correction_id != reversal_id
    with sqlite3.connect(database_path) as connection:
        assert_spend_event_shadow_rows(connection)
        assert tuple(connection.execute(
            f"SELECT base_usd, {nano_ledger_module.SPEND_EVENT_NANO_COLUMNS['base_usd']} FROM spend_events WHERE id=?",
                                        (event_id,)).fetchone()) == (None, None)
    nano_column = nano_ledger_module.SPEND_EVENT_NANO_COLUMNS['batch_usd']
    with pytest.raises(ValueError, match="derived"):
        ledger.update(event_id, {nano_column: 0})
    with pytest.raises(ValueError, match="immutable"):
        ledger.update(event_id, {"currency": NANO_FIXTURE_OTHER_CURRENCY})
    for column in nano_ledger_module.SPEND_EVENT_NANO_COLUMNS:
        ledger.update(event_id, {column: NANO_FIXTURE_PRIMARY_AMOUNT})
    with sqlite3.connect(database_path) as connection:
        assert_spend_event_shadow_rows(connection)
    for column in nano_ledger_module.SPEND_EVENT_NANO_COLUMNS:
        ledger.update(event_id, {column: None})
    ledger.record_event({"source": __name__, "dedup_key": f"{__name__}:zero", "cost_basis": "unpriced",
                         "batch_usd": NANO_FIXTURE_ZERO})
    with sqlite3.connect(database_path) as connection:
        assert_spend_event_shadow_rows(connection)


def test_spend_event_nano_owned_connection_migration_and_nested_rollback():
    connection = sqlite3.connect(NANO_FIXTURE_MEMORY_DATABASE)
    connection.execute(nano_ledger_module._DDL_EVENTS)
    connection.execute("INSERT INTO spend_events(id,currency,batch_usd) VALUES(?,?,?)",
                       (__name__, NANO_FIXTURE_CURRENCY, NANO_FIXTURE_PRIMARY_AMOUNT))
    connection.commit()
    ledger = nano_ledger_module.SpendLedger(NANO_FIXTURE_MEMORY_DATABASE, owned_connection=connection)
    assert_spend_event_shadow_rows(connection)
    connection.close()
    # Exercise failure with an ambient transaction; constructor owns/ closes its supplied connection,
    # so call the initializer directly to inspect transaction containment without losing the handle.
    connection = sqlite3.connect(NANO_FIXTURE_MEMORY_DATABASE)
    connection.row_factory = sqlite3.Row
    connection.create_aggregate("dec_sum", 1, nano_ledger_module._DecSum)
    connection.execute(nano_ledger_module._DDL_EVENTS)
    connection.execute("INSERT INTO spend_events(id,currency,batch_usd) VALUES(?,?,?)",
                       (__name__, NANO_FIXTURE_CURRENCY, NANO_FIXTURE_BAD_AMOUNTS[0]))
    ledger._conn = connection
    with pytest.raises(ValueError, match="invalid_amount_gap"):
        ledger._initialize_spend_schema()
    assert connection.in_transaction
    columns = {row[1] for row in connection.execute("PRAGMA table_info(spend_events)")}
    assert not columns.intersection(nano_ledger_module.SPEND_EVENT_NANO_COLUMNS.values())
    assert connection.execute("SELECT batch_usd FROM spend_events").fetchone()[0] == NANO_FIXTURE_BAD_AMOUNTS[0]
    connection.close()


def test_spend_event_nano_aggregate_exceeding_sqlite_integer_stays_exact(tmp_path):
    database_path = tmp_path / NANO_FIXTURE_DB_NAME
    with sqlite3.connect(database_path) as connection:
        connection.execute(nano_ledger_module._DDL_EVENTS)
        for row_index in range(NANO_FIXTURE_AGGREGATE_OVERFLOW_ROWS):
            connection.execute("INSERT INTO spend_events(id,currency,batch_usd) VALUES(?,?,?)",
                               (f"{__name__}:{row_index}", NANO_FIXTURE_CURRENCY,
                                NANO_FIXTURE_AGGREGATE_OVERFLOW_AMOUNT))
    nano_ledger_module.SpendLedger(database_path)
    with sqlite3.connect(database_path) as connection:
        assert_spend_event_shadow_rows(connection)
        column = nano_ledger_module.SPEND_EVENT_NANO_COLUMNS['batch_usd']
        total = sum(row[0] for row in connection.execute(f"SELECT {column} FROM spend_events"))
        reference = Decimal(NANO_FIXTURE_AGGREGATE_OVERFLOW_AMOUNT) * NANO_FIXTURE_AGGREGATE_OVERFLOW_ROWS
        assert_nano_total_matches(total, reference, label=__name__)


if __name__ == "__main__":
    raise SystemExit(run_pytest_style(globals()))
