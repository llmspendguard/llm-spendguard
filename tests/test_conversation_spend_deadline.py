"""Conversation consumer chaos: bounded cold reads, slow complete scans, recovery."""
import concurrent.futures
import sqlite3
import sys
import tempfile
import threading
import time
from pathlib import Path
from unittest.mock import patch

from _pytest_fixture_bridge import align_test_startup_home, run_pytest_style

align_test_startup_home(prefix=__name__)

from spendguard import budget, config, ledger, ledger_read
import spendguard
from spendguard.money_scale import NANO_USD_PER_USD

CONVERSATION_FAULT_ROWS = 2048
CONVERSATION_FAULT_WORKERS = 4
CONVERSATION_FAULT_DEADLINE_S = 0.02
CONVERSATION_FAULT_BOUND_S = 1.0
CONVERSATION_FAULT_STEP_S = 0.001
CONVERSATION_COVERAGE_DELAY_MULTIPLIER = 2
CONVERSATION_FAULT_FAST_S = 5.0
CONVERSATION_LOCK_EARLY_BUSY_TIMEOUT_S = 0.0
CONVERSATION_NON_BUSY_OPEN_CODES = (sqlite3.SQLITE_LOCKED, sqlite3.SQLITE_CANTOPEN)
CONVERSATION_FAULT_INVALID_DEADLINES = (float("nan"), float("inf"), 0.0, -1.0)
CONVERSATION_FAULT_ID = "consumer-deadline-conversation"
CONVERSATION_FAULT_DAY = budget._utc().strftime("%Y-%m-%d")
CONVERSATION_FAULT_INSERT = """INSERT OR IGNORE INTO spend_events
    (id,day,source,realtime_usd,conv_id,status,is_meta,reconciled)
    VALUES (?,?,'consumer-deadline','1',?,'posted',0,0)"""
CONVERSATION_FAULT_ORIGINAL_STEP = ledger._DecSum.step


def slow_conversation_consumer_decimal_step(self, value):
    time.sleep(CONVERSATION_FAULT_STEP_S)
    CONVERSATION_FAULT_ORIGINAL_STEP(self, value)


def seed_conversation_consumer_fault_rows():
    seed = ledger.SpendLedger(owned_connection=config.open_ledger_connection())
    seed._conn.executemany(CONVERSATION_FAULT_INSERT, (
        (str(index), CONVERSATION_FAULT_DAY, CONVERSATION_FAULT_ID)
        for index in range(CONVERSATION_FAULT_ROWS)))
    seed._conn.execute(
        f"UPDATE spend_events SET {ledger.SPEND_EVENT_NANO_COLUMNS['realtime_usd']}=? WHERE conv_id=?",
        (NANO_USD_PER_USD, CONVERSATION_FAULT_ID))
    seed._conn.commit()
    seed._conn.close()
    from spendguard.ledger_writer import close_ledger_writers
    close_ledger_writers()


def test_conversation_spend_deadline_slow_concurrent_recovery():
    seed_conversation_consumer_fault_rows()
    baseline = config.ledger_connection_count()
    barrier = threading.Barrier(CONVERSATION_FAULT_WORKERS)

    def exercise_conversation_consumer_deadline():
        barrier.wait(timeout=CONVERSATION_FAULT_BOUND_S)
        started = time.monotonic()
        try:
            budget.spent_since(CONVERSATION_FAULT_DAY, conv=CONVERSATION_FAULT_ID,
                               deadline_s=CONVERSATION_FAULT_DEADLINE_S)
        except ledger_read.LedgerQueryDeadline:
            pass
        else:
            raise AssertionError("slow complete conversation aggregate escaped its deadline")
        assert time.monotonic() - started < CONVERSATION_FAULT_BOUND_S

    with patch.object(ledger._DecSum, "step", slow_conversation_consumer_decimal_step):
        with concurrent.futures.ThreadPoolExecutor(max_workers=CONVERSATION_FAULT_WORKERS) as pool:
            for future in [pool.submit(exercise_conversation_consumer_deadline)
                           for _ in range(CONVERSATION_FAULT_WORKERS)]:
                future.result(timeout=CONVERSATION_FAULT_BOUND_S)
    assert config.ledger_connection_count() == baseline
    assert budget.spent_since(CONVERSATION_FAULT_DAY, conv=CONVERSATION_FAULT_ID,
                              deadline_s=CONVERSATION_FAULT_FAST_S) == CONVERSATION_FAULT_ROWS
    assert config.ledger_connection_count() == baseline


def test_conversation_spend_deadline_cold_read_never_initializes_writer():
    seed_conversation_consumer_fault_rows()
    # A write constructor may migrate and lock; a consumer must never enter it.
    with patch.object(ledger.SpendLedger, "__init__", side_effect=AssertionError("write initialization")):
        assert budget.spent_since(CONVERSATION_FAULT_DAY, conv=CONVERSATION_FAULT_ID,
                                  deadline_s=CONVERSATION_FAULT_FAST_S) == CONVERSATION_FAULT_ROWS


def test_unscoped_consumer_snapshot_while_writer_transaction_is_open():
    """A cold consumer's ordinary API call must never request a writer handoff."""
    seed_conversation_consumer_fault_rows()
    writer = config.open_ledger_connection()
    try:
        writer.execute("BEGIN IMMEDIATE")

        def query_cold_consumer_snapshot():
            assert getattr(budget._LEDGER_TL, "led", None) is None
            with patch.object(ledger.SpendLedger, "__init__",
                              side_effect=AssertionError("consumer initialized write ledger")):
                automatic = budget.spent_since(CONVERSATION_FAULT_DAY,
                                               deadline_s=CONVERSATION_FAULT_DEADLINE_S)
                explicit = budget.spent_since(CONVERSATION_FAULT_DAY, read_only=True,
                                              deadline_s=CONVERSATION_FAULT_FAST_S)
                assert automatic == explicit == CONVERSATION_FAULT_ROWS
                assert {"capability_ledger_read_budget_spent_since",
                        "capability_ledger_read_ledger_read_read_spend_since"} <= set(spendguard.__all__)
                assert spendguard.capability_ledger_read_budget_spent_since(CONVERSATION_FAULT_DAY,
                                               deadline_s=CONVERSATION_FAULT_FAST_S) == automatic
                assert spendguard.capability_ledger_read_ledger_read_read_spend_since(CONVERSATION_FAULT_DAY,
                                                   deadline_s=CONVERSATION_FAULT_FAST_S) == automatic
                assert budget.spent_by_job(CONVERSATION_FAULT_ID,
                                           deadline_s=CONVERSATION_FAULT_FAST_S) == 0
                assert budget.spent_all_time(deadline_s=CONVERSATION_FAULT_FAST_S) == automatic
                assert sum(budget.by_day().values()) == automatic
                assert budget.input_cache_split()["input_tok"] == 0
            assert getattr(budget._LEDGER_TL, "led", None) is None

        with concurrent.futures.ThreadPoolExecutor() as pool:
            pool.submit(query_cold_consumer_snapshot).result(timeout=CONVERSATION_FAULT_FAST_S)
        assert writer.in_transaction
    finally:
        writer.rollback()
        writer.close()


def test_initialized_budget_owner_retains_writer_facade_fast_path():
    seed_conversation_consumer_fault_rows()
    budget._reset_ledger()
    owner_facade = budget._ledger()
    try:
        with patch.object(budget, "_ledger", return_value=owner_facade) as owner_lookup:
            ordinary = budget.spent_since(CONVERSATION_FAULT_DAY,
                                          deadline_s=CONVERSATION_FAULT_FAST_S)
            owner_lookup.assert_called_once_with()
        assert ordinary == CONVERSATION_FAULT_ROWS
        assert budget.spent_since(CONVERSATION_FAULT_DAY, read_only=True,
                                  deadline_s=CONVERSATION_FAULT_FAST_S) == ordinary
        with patch.object(budget, "_ledger", side_effect=AssertionError("forced snapshot used writer")):
            assert spendguard.capability_ledger_read_ledger_read_read_spend_since(CONVERSATION_FAULT_DAY,
                                               deadline_s=CONVERSATION_FAULT_FAST_S) == ordinary
    finally:
        budget._reset_ledger()


def test_conversation_settlement_coverage_shares_spend_deadline():
    seed_conversation_consumer_fault_rows()

    def delay_conversation_settlement_coverage(*args, **kwargs):
        time.sleep(CONVERSATION_FAULT_DEADLINE_S * CONVERSATION_COVERAGE_DELAY_MULTIPLIER)
        return {"n": 0, "input_cost_floor": None}

    with patch.object(ledger_read, "_read_incomplete_settlement_totals",
                      side_effect=delay_conversation_settlement_coverage):
        try:
            budget.spent_since(CONVERSATION_FAULT_DAY, conv=CONVERSATION_FAULT_ID,
                               deadline_s=CONVERSATION_FAULT_DEADLINE_S)
        except ledger_read.LedgerQueryDeadline:
            pass
        else:
            raise AssertionError("settlement coverage must consume the same spend deadline")


def exercise_conversation_exclusive_lock_refusal(*, early_busy, deadline_s):
    seed_conversation_consumer_fault_rows()
    baseline = config.ledger_connection_count()
    original_open = config.open_ledger_connection

    def open_conversation_lock_early_busy(*args, **kwargs):
        # SQLite's millisecond busy timeout may finish before the caller's
        # monotonic deadline. Force that ordering while retaining a real lock.
        if early_busy:
            kwargs["timeout"] = CONVERSATION_LOCK_EARLY_BUSY_TIMEOUT_S
        return original_open(*args, **kwargs)

    lock_connection = sqlite3.connect(config.db_path())
    try:
        lock_connection.execute("PRAGMA journal_mode=DELETE")
        lock_connection.execute("PRAGMA locking_mode=EXCLUSIVE")
        lock_connection.execute("BEGIN EXCLUSIVE")
        started = time.monotonic()
        try:
            with patch.object(config, "open_ledger_connection", open_conversation_lock_early_busy):
                budget.spent_since(CONVERSATION_FAULT_DAY, conv=CONVERSATION_FAULT_ID,
                                   deadline_s=deadline_s)
        except ledger_read.LedgerQueryDeadline:
            pass
        else:
            raise AssertionError("locked conversation read must refuse explicitly")
        assert time.monotonic() - started < CONVERSATION_FAULT_BOUND_S
        assert lock_connection.in_transaction
        assert config.ledger_connection_count() == baseline
    finally:
        lock_connection.rollback()
        lock_connection.close()
    assert budget.spent_since(CONVERSATION_FAULT_DAY, conv=CONVERSATION_FAULT_ID,
                              deadline_s=CONVERSATION_FAULT_FAST_S) == CONVERSATION_FAULT_ROWS


def test_conversation_spend_deadline_exclusive_lock():
    exercise_conversation_exclusive_lock_refusal(
        early_busy=False, deadline_s=CONVERSATION_FAULT_DEADLINE_S)


def test_conversation_spend_deadline_exclusive_lock_early_busy():
    exercise_conversation_exclusive_lock_refusal(
        early_busy=True, deadline_s=CONVERSATION_FAULT_FAST_S)


def test_conversation_spend_deadline_non_busy_open_errors():
    for error_code in CONVERSATION_NON_BUSY_OPEN_CODES:
        open_error = sqlite3.OperationalError()
        open_error.sqlite_errorcode = error_code
        with patch.object(config, "open_ledger_connection", side_effect=open_error):
            try:
                budget.spent_since(CONVERSATION_FAULT_DAY, conv=CONVERSATION_FAULT_ID,
                                   deadline_s=CONVERSATION_FAULT_FAST_S)
            except ledger_read.LedgerReadOpenError as refusal:
                assert type(refusal) is ledger_read.LedgerReadOpenError
                assert refusal.__cause__ is open_error
            else:
                raise AssertionError("non-busy open failure must preserve its typed refusal")


def test_conversation_spend_deadline_invalid_durations_refuse():
    for invalid_deadline in CONVERSATION_FAULT_INVALID_DEADLINES:
        try:
            budget.spent_since(CONVERSATION_FAULT_DAY, conv=CONVERSATION_FAULT_ID,
                               deadline_s=invalid_deadline)
        except ledger_read.LedgerQueryDeadline:
            pass
        else:
            raise AssertionError("invalid conversation deadline must refuse")


def test_conversation_spend_deadline_busy_admission_refuses():
    seed_conversation_consumer_fault_rows()
    with concurrent.futures.ThreadPoolExecutor(max_workers=CONVERSATION_FAULT_WORKERS) as pool:
        with config._LEDGER_ADMISSION.lock:
            started = time.monotonic()
            future = pool.submit(budget.spent_since, CONVERSATION_FAULT_DAY,
                                 conv=CONVERSATION_FAULT_ID,
                                 deadline_s=CONVERSATION_FAULT_DEADLINE_S)
            try:
                future.result(timeout=CONVERSATION_FAULT_BOUND_S)
            except ledger_read.LedgerQueryDeadline:
                pass
            else:
                raise AssertionError("busy connection admission must refuse")
            assert time.monotonic() - started < CONVERSATION_FAULT_BOUND_S


def test_conversation_spend_deadline_concurrent_admission_accounts_once():
    seed_conversation_consumer_fault_rows()
    baseline = config.ledger_connection_count()
    barrier = threading.Barrier(CONVERSATION_FAULT_WORKERS)
    original_memory_tuning = config.budget_ledger_connection_memory

    def synchronize_conversation_connection_setup(connection, ceiling=None):
        barrier.wait(timeout=CONVERSATION_FAULT_BOUND_S)
        return original_memory_tuning(connection, ceiling=ceiling)

    handles = []
    try:
        with patch.object(config, "ledger_connection_limit", return_value=baseline + CONVERSATION_FAULT_WORKERS):
            with patch.object(config, "budget_ledger_connection_memory", synchronize_conversation_connection_setup):
                with concurrent.futures.ThreadPoolExecutor(max_workers=CONVERSATION_FAULT_WORKERS) as pool:
                    futures = [pool.submit(config.open_ledger_connection, config.db_path(), tune=False,
                                           deadline_at=time.monotonic() + CONVERSATION_FAULT_FAST_S)
                               for _ in range(CONVERSATION_FAULT_WORKERS)]
                    for future in futures:
                        handles.append(future.result(timeout=CONVERSATION_FAULT_FAST_S))
        assert config.ledger_connection_count() == baseline + CONVERSATION_FAULT_WORKERS
    finally:
        for handle in handles:
            handle.close()
    assert config.ledger_connection_count() == baseline


def test_consumer_empty_ledger_compatibility_never_creates_persistent_schema():
    with tempfile.TemporaryDirectory() as consumer_directory:
        consumer_path = Path(consumer_directory) / Path(config.db_path()).name
        budget._reset_ledger()
        try:
            with patch.object(config, "db_path", return_value=consumer_path), \
                    patch.object(ledger.SpendLedger, "__init__", side_effect=AssertionError("consumer initialized writer")):
                assert budget.spent_since(CONVERSATION_FAULT_DAY) == 0
                assert not consumer_path.exists()
                with sqlite3.connect(consumer_path) as calls_only_connection:
                    calls_only_connection.execute("CREATE TABLE calls (id TEXT)")
                ledger_bytes_before = consumer_path.read_bytes()
                assert budget.spent_since(CONVERSATION_FAULT_DAY) == 0
                assert budget.spent_since(CONVERSATION_FAULT_DAY, read_only=True) == 0
                assert budget.by_basis(CONVERSATION_FAULT_DAY) == {}
                assert consumer_path.read_bytes() == ledger_bytes_before
                with sqlite3.connect(consumer_path) as schema_connection:
                    assert schema_connection.execute(
                        "SELECT name FROM sqlite_master WHERE name=?", (ledger_read.LEDGER_EVENTS_TABLE,)
                    ).fetchone() is None
        finally:
            budget._reset_ledger()


if __name__ == "__main__":
    sys.exit(run_pytest_style(globals()))
