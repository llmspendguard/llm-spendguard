"""Conversation consumer chaos: bounded cold reads, slow complete scans, recovery."""
import concurrent.futures
import sqlite3
import sys
import threading
import time
from unittest.mock import patch

from _pytest_fixture_bridge import align_test_startup_home, run_pytest_style

align_test_startup_home(prefix=__name__)

from spendguard import budget, config, ledger, ledger_read

CONVERSATION_FAULT_ROWS = 2048
CONVERSATION_FAULT_WORKERS = 4
CONVERSATION_FAULT_DEADLINE_S = 0.02
CONVERSATION_FAULT_BOUND_S = 1.0
CONVERSATION_FAULT_STEP_S = 0.001
CONVERSATION_FAULT_FAST_S = 5.0
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
    seed = ledger.SpendLedger()
    seed._conn.executemany(CONVERSATION_FAULT_INSERT, (
        (str(index), CONVERSATION_FAULT_DAY, CONVERSATION_FAULT_ID)
        for index in range(CONVERSATION_FAULT_ROWS)))
    seed._conn.commit()
    seed._conn.close()


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


def test_conversation_spend_deadline_exclusive_lock():
    seed_conversation_consumer_fault_rows()
    lock_connection = sqlite3.connect(config.db_path())
    try:
        lock_connection.execute("PRAGMA journal_mode=DELETE")
        lock_connection.execute("PRAGMA locking_mode=EXCLUSIVE")
        lock_connection.execute("BEGIN EXCLUSIVE")
        started = time.monotonic()
        try:
            budget.spent_since(CONVERSATION_FAULT_DAY, conv=CONVERSATION_FAULT_ID,
                               deadline_s=CONVERSATION_FAULT_DEADLINE_S)
        except ledger_read.LedgerQueryDeadline:
            pass
        else:
            raise AssertionError("locked conversation read must refuse explicitly")
        assert time.monotonic() - started < CONVERSATION_FAULT_BOUND_S
    finally:
        lock_connection.rollback()
        lock_connection.close()


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


if __name__ == "__main__":
    sys.exit(run_pytest_style(globals()))
