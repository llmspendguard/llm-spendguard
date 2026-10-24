"""Fault guards: complete seeded scans, deadline refusal, recovery and concurrency."""
import concurrent.futures
import sqlite3
import sys
import threading
import time
from unittest.mock import Mock, patch

from _pytest_fixture_bridge import align_test_startup_home, run_pytest_style

align_test_startup_home(prefix=__name__)

from spendguard import budget, config, ledger, ledger_read

DEADLINE_GUARD_ROWS = 512
DEADLINE_GUARD_STEP_S = 0.001
DEADLINE_GUARD_BUDGET_S = 0.01
DEADLINE_GUARD_WORKERS = 4
# Parallel gate load can serialize each worker's connection recovery. Bound the
# whole group by the configured per-ledger timeout per worker, not one host-speed
# wall clock shared by all workers.
DEADLINE_GUARD_OPERATION_TIMEOUT_S = config.LEDGER_WRITE_TIMEOUT_S * DEADLINE_GUARD_WORKERS
DEADLINE_GUARD_LOAD_MULTIPLIER = 10
DEADLINE_GUARD_MINIMUM_RECOVERY_S = config.LEDGER_CONNECT_TIMEOUT_S
DEADLINE_GUARD_SHARED_SCAN_MULTIPLIER = 1.5
DEADLINE_GUARD_DAY = budget._utc().strftime("%Y-%m-%d")
DEADLINE_GUARD_SOURCE = "progress-deadline-guard"
DEADLINE_GUARD_AMOUNT = "1"
DEADLINE_GUARD_PENDING_AMOUNT = "0"
DEADLINE_GUARD_JOB = "deadline-guard-job"
DEADLINE_GUARD_EMPTY_JOB = "deadline-guard-empty-job"
DEADLINE_GUARD_FAILURE_MESSAGE = "injected deadline replacement failure"
DEADLINE_GUARD_PENDING_ID = "deadline-guard-pending-write"
DEADLINE_GUARD_AUDIT_SQL = "SELECT * FROM spend_audit ORDER BY id"
DEADLINE_GUARD_ORIGINAL_STEP = ledger._DecSum.step
DEADLINE_GUARD_INSERT_SQL = """INSERT OR IGNORE INTO spend_events
    (id, day, source, realtime_usd, chain, status, is_meta, reconciled)
    VALUES (?, ?, ?, ?, ?, 'posted', 0, 0)"""
DEADLINE_GUARD_SNAPSHOT_SQL = "SELECT * FROM spend_events ORDER BY id"


def slow_deadline_guard_decimal_step(self, value):
    time.sleep(DEADLINE_GUARD_STEP_S)
    DEADLINE_GUARD_ORIGINAL_STEP(self, value)


def seed_deadline_guard_ledger():
    current = budget._ledger()
    current._conn.executemany(DEADLINE_GUARD_INSERT_SQL, (
        (str(index), DEADLINE_GUARD_DAY, DEADLINE_GUARD_SOURCE,
         DEADLINE_GUARD_AMOUNT, DEADLINE_GUARD_JOB)
        for index in range(DEADLINE_GUARD_ROWS)))
    current._conn.commit()
    return current


def capture_deadline_guard_rows(current):
    return tuple(tuple(row) for row in current._conn.execute(DEADLINE_GUARD_SNAPSHOT_SQL))


def close_deadline_guard_thread_ledger():
    current = getattr(budget._LEDGER_TL, "led", None)
    if current is not None and current._conn is not None:
        current._conn.close()
    budget._reset_ledger()


def measure_deadline_guard_recovery(deadline_query):
    """Measure the successful query, then derive headroom from this machine's observed load."""
    started = time.monotonic()
    result = deadline_query()
    elapsed = time.monotonic() - started
    deadline_s = max(DEADLINE_GUARD_MINIMUM_RECOVERY_S, elapsed * DEADLINE_GUARD_LOAD_MULTIPLIER)
    return result, deadline_s


def test_ledger_query_deadline_fault_and_recovery():
    current = seed_deadline_guard_ledger()
    before = capture_deadline_guard_rows(current)
    expired_connection = current._conn
    started = time.monotonic()
    try:
        with patch.object(ledger._DecSum, "step", slow_deadline_guard_decimal_step):
            try:
                budget.spent_since(DEADLINE_GUARD_DAY, deadline_s=DEADLINE_GUARD_BUDGET_S)
            except ledger_read.LedgerQueryDeadline:
                pass
            else:
                raise AssertionError("slow complete aggregate must raise the typed deadline error")
        assert time.monotonic() - started < DEADLINE_GUARD_OPERATION_TIMEOUT_S
        recovered = budget._ledger()
        assert recovered._conn is not expired_connection
        try:
            expired_connection.execute("SELECT 1")
        except sqlite3.ProgrammingError:
            pass
        else:
            raise AssertionError("expired connection must be closed, not retained")
        recovered_total, recovery_deadline_s = measure_deadline_guard_recovery(
            lambda: budget.spent_since(DEADLINE_GUARD_DAY))
        assert recovered_total == DEADLINE_GUARD_ROWS
        assert budget.spent_since(DEADLINE_GUARD_DAY, deadline_s=recovery_deadline_s) == DEADLINE_GUARD_ROWS
        assert budget.spent_by_job(DEADLINE_GUARD_JOB, deadline_s=recovery_deadline_s) == DEADLINE_GUARD_ROWS
        assert budget.spent_all_time(deadline_s=recovery_deadline_s) == DEADLINE_GUARD_ROWS
        assert capture_deadline_guard_rows(recovered) == before
    finally:
        close_deadline_guard_thread_ledger()


def test_ledger_query_deadline_concurrent_pool_baseline():
    seed_deadline_guard_ledger()
    close_deadline_guard_thread_ledger()
    baseline = config.ledger_connection_count()
    barrier = threading.Barrier(DEADLINE_GUARD_WORKERS)

    def contend_deadline_guard_reader():
        current = budget._ledger()
        expired_connection = current._conn
        barrier.wait(timeout=DEADLINE_GUARD_OPERATION_TIMEOUT_S)
        try:
            try:
                budget.spent_since(DEADLINE_GUARD_DAY, deadline_s=DEADLINE_GUARD_BUDGET_S)
            except ledger_read.LedgerQueryDeadline:
                pass
            else:
                raise AssertionError("every concurrent scan must expire")
            assert budget._ledger()._conn is not expired_connection
            # Empty WHERE proves immediate usability without another slow full aggregate.
            assert budget.spent_by_job(DEADLINE_GUARD_EMPTY_JOB) == 0
        finally:
            close_deadline_guard_thread_ledger()

    started = time.monotonic()
    with patch.object(ledger._DecSum, "step", slow_deadline_guard_decimal_step):
        with concurrent.futures.ThreadPoolExecutor(max_workers=DEADLINE_GUARD_WORKERS) as executor:
            futures = [executor.submit(contend_deadline_guard_reader) for _ in range(DEADLINE_GUARD_WORKERS)]
            for future in futures:
                future.result(timeout=DEADLINE_GUARD_OPERATION_TIMEOUT_S)
    assert time.monotonic() - started < DEADLINE_GUARD_OPERATION_TIMEOUT_S
    assert config.ledger_connection_count() == baseline


def test_ledger_query_deadline_budget_refuses():
    seed_deadline_guard_ledger()
    try:
        with patch.object(ledger._DecSum, "step", slow_deadline_guard_decimal_step):
            refusal = budget.exceeded(deadline_s=DEADLINE_GUARD_BUDGET_S)
        assert refusal is not None, "deadline must refuse even without a configured cap"
    finally:
        close_deadline_guard_thread_ledger()



def test_ledger_query_deadline_preserves_pending_write():
    current = seed_deadline_guard_ledger()
    connection = current._conn
    try:
        audit_before = tuple(tuple(row) for row in connection.execute(DEADLINE_GUARD_AUDIT_SQL))
        connection.execute(DEADLINE_GUARD_INSERT_SQL, (
            DEADLINE_GUARD_PENDING_ID, DEADLINE_GUARD_DAY, DEADLINE_GUARD_SOURCE,
            DEADLINE_GUARD_PENDING_AMOUNT, DEADLINE_GUARD_JOB))
        pending_rows = capture_deadline_guard_rows(current)
        assert connection.in_transaction
        try:
            budget.spent_since(DEADLINE_GUARD_DAY, deadline_s=DEADLINE_GUARD_BUDGET_S)
        except ledger_read.LedgerQueryDeadline:
            pass
        else:
            raise AssertionError("deadline read must refuse before interrupting a pending write")
        assert current._conn is connection and connection.in_transaction
        assert capture_deadline_guard_rows(current) == pending_rows
        assert tuple(tuple(row) for row in connection.execute(DEADLINE_GUARD_AUDIT_SQL)) == audit_before
        connection.commit()
        assert capture_deadline_guard_rows(current) == pending_rows
    finally:
        close_deadline_guard_thread_ledger()



def test_ledger_query_deadline_replacement_fault_recovers():
    for failure_mode in ("open", "aggregate-registration"):
        current = seed_deadline_guard_ledger()
        before = capture_deadline_guard_rows(current)
        baseline = config.ledger_connection_count()
        original_open = config.open_ledger_connection
        failed_handles = []

        def fail_deadline_guard_replacement(*args, mode=failure_mode, opener=original_open,
                                            handles=failed_handles, **kwargs):
            if mode == "open":
                raise sqlite3.OperationalError(DEADLINE_GUARD_FAILURE_MESSAGE)
            raw = opener(*args, **kwargs)
            handles.append(raw)
            proxy = Mock(wraps=raw)
            proxy.create_aggregate.side_effect = sqlite3.OperationalError(DEADLINE_GUARD_FAILURE_MESSAGE)
            return proxy

        try:
            with patch.object(ledger._DecSum, "step", slow_deadline_guard_decimal_step), \
                    patch.object(config, "open_ledger_connection", side_effect=fail_deadline_guard_replacement):
                try:
                    budget.spent_since(DEADLINE_GUARD_DAY, deadline_s=DEADLINE_GUARD_BUDGET_S)
                except ledger_read.LedgerQueryDeadline:
                    pass
                else:
                    raise AssertionError("replacement failure must remain a typed refusal")
                assert budget.exceeded(deadline_s=DEADLINE_GUARD_BUDGET_S) is not None
            assert config.ledger_connection_count() < baseline
            for failed in failed_handles:
                try:
                    failed.execute("SELECT 1")
                except sqlite3.ProgrammingError:
                    pass
                else:
                    raise AssertionError("partially configured replacement handle leaked")
            assert budget.spent_all_time() == DEADLINE_GUARD_ROWS
            recovered_total, recovery_deadline_s = measure_deadline_guard_recovery(
                lambda: budget.spent_since(DEADLINE_GUARD_DAY))
            assert recovered_total == DEADLINE_GUARD_ROWS
            assert budget.spent_since(DEADLINE_GUARD_DAY, deadline_s=recovery_deadline_s) == DEADLINE_GUARD_ROWS
            assert capture_deadline_guard_rows(current) == before
            assert config.ledger_connection_count() == baseline
        finally:
            close_deadline_guard_thread_ledger()



def test_ledger_query_deadline_shared_budget_window():
    seed_deadline_guard_ledger()
    try:
        with patch.object(ledger._DecSum, "step", slow_deadline_guard_decimal_step):
            started = time.monotonic()
            with ledger_read.ledger_request_scope(
                    deadline_s=DEADLINE_GUARD_OPERATION_TIMEOUT_S):
                assert budget.spent_since(
                    DEADLINE_GUARD_DAY,
                    deadline_s=DEADLINE_GUARD_OPERATION_TIMEOUT_S,
                ) == DEADLINE_GUARD_ROWS
            scan_duration = time.monotonic() - started
            shared_budget = scan_duration * DEADLINE_GUARD_SHARED_SCAN_MULTIPLIER
            started = time.monotonic()
            refusal = budget.exceeded(deadline_s=shared_budget)
            elapsed = time.monotonic() - started
        assert refusal is not None, "daily and monthly scans must share one caller deadline"
        assert elapsed < shared_budget + DEADLINE_GUARD_OPERATION_TIMEOUT_S
    finally:
        close_deadline_guard_thread_ledger()


if __name__ == "__main__":
    sys.exit(run_pytest_style(globals()))
