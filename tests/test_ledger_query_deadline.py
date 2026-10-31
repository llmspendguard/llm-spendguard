"""Fault guards: complete seeded scans, deadline refusal, recovery and concurrency."""
import concurrent.futures
import itertools
import sqlite3
import sys
import threading
import time
from contextlib import contextmanager, ExitStack
from types import SimpleNamespace
from unittest.mock import Mock, patch

from _pytest_fixture_bridge import align_test_startup_home, run_pytest_style

align_test_startup_home(prefix=__name__)

from spendguard import budget, config, ledger, ledger_read, ledger_writer

DEADLINE_GUARD_ROWS = 512
DEADLINE_GUARD_SCAN_S = 1.0
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
DEADLINE_GUARD_MONTH = budget._utc().strftime("%Y-%m-01")
DEADLINE_GUARD_SOURCE = "progress-deadline-guard"
DEADLINE_GUARD_AMOUNT = "1"
DEADLINE_GUARD_PENDING_AMOUNT = "7"
DEADLINE_GUARD_JOB = "deadline-guard-job"
DEADLINE_GUARD_EMPTY_JOB = "deadline-guard-empty-job"
DEADLINE_GUARD_FAILURE_MESSAGE = "injected deadline replacement failure"
DEADLINE_GUARD_OBSERVER_OWNER_COUNT = 2
DEADLINE_GUARD_BEFORE_OBSERVER_OPEN = "before-open"
DEADLINE_GUARD_ENTERED_OBSERVER_OWNER = "entered-owner"
DEADLINE_GUARD_OBSERVER_GENERATION_READ = "generation-read"
DEADLINE_GUARD_PENDING_ID = "deadline-guard-pending-write"
DEADLINE_GUARD_AUDIT_SQL = "SELECT * FROM spend_audit ORDER BY id"
DEADLINE_GUARD_POSTED_STATUS = "posted"
DEADLINE_GUARD_SNAPSHOT_SQL = "SELECT * FROM spend_events ORDER BY id"
DEADLINE_GUARD_DELETE_PENDING_SQL = "DELETE FROM spend_events WHERE id=?"


@contextmanager
def advance_deadline_guard_on_sqlite_progress():
    """Charge virtual scan time on the first real VM poll, then run the real guard."""
    clock_state = threading.local()
    original_aggregate_step = ledger._DecSum.step

    def deadline_guard_virtual_monotonic():
        return time.monotonic() + getattr(clock_state, "elapsed", 0.0)

    def charge_deadline_guard_aggregate(aggregate, value):
        if not getattr(aggregate, "_deadline_guard_charged", False):
            aggregate._deadline_guard_charged = True
            clock_state.elapsed = getattr(clock_state, "elapsed", 0.0) + DEADLINE_GUARD_SCAN_S
        return original_aggregate_step(aggregate, value)

    clock_proxy = SimpleNamespace(**{name: getattr(time, name) for name in dir(time)})
    clock_proxy.monotonic = deadline_guard_virtual_monotonic
    with ExitStack() as patches:
        for module in (ledger, ledger_read, config, budget):
            patches.enter_context(patch.object(module, "time", clock_proxy))
        patches.enter_context(patch.object(ledger._DecSum, "step", charge_deadline_guard_aggregate))
        yield clock_proxy


def record_deadline_guard_fixture_event(current, event_id, amount):
    return current.record_event(dict(id=event_id, day=DEADLINE_GUARD_DAY,
        source=DEADLINE_GUARD_SOURCE, realtime_usd=amount, chain=DEADLINE_GUARD_JOB,
        status=DEADLINE_GUARD_POSTED_STATUS))


def seed_deadline_guard_ledger():
    current = budget._ledger()
    if current._conn is None:
        current._restore_spend_aggregate_connection()
        current._explicit_owned_connection = True
    for index in range(DEADLINE_GUARD_ROWS):
        record_deadline_guard_fixture_event(current, str(index), DEADLINE_GUARD_AMOUNT)
    current._conn.commit()
    return current


def capture_deadline_guard_rows(current):
    if current._conn is not None:
        return tuple(tuple(row) for row in current._conn.execute(DEADLINE_GUARD_SNAPSHOT_SQL))
    with config.managed_ledger_read_connection(current.db_path) as connection:
        return tuple(tuple(row) for row in connection.execute(DEADLINE_GUARD_SNAPSHOT_SQL))


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
        with advance_deadline_guard_on_sqlite_progress():
            try:
                budget.spent_since(DEADLINE_GUARD_DAY, deadline_s=DEADLINE_GUARD_BUDGET_S)
            except ledger_read.LedgerQueryDeadline:
                pass
            else:
                raise AssertionError("slow complete aggregate must raise the typed deadline error")
        assert time.monotonic() - started < DEADLINE_GUARD_OPERATION_TIMEOUT_S
        recovered = budget._ledger()
        assert recovered._conn is expired_connection
        assert expired_connection.execute("SELECT 1").fetchone()[0] == 1
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
            assert expired_connection is None
            assert budget._ledger()._conn is None
            # Empty WHERE proves immediate usability without another slow full aggregate.
            assert budget.spent_by_job(DEADLINE_GUARD_EMPTY_JOB) == 0
        finally:
            close_deadline_guard_thread_ledger()

    started = time.monotonic()
    with advance_deadline_guard_on_sqlite_progress():
        with concurrent.futures.ThreadPoolExecutor(max_workers=DEADLINE_GUARD_WORKERS) as executor:
            futures = [executor.submit(contend_deadline_guard_reader) for _ in range(DEADLINE_GUARD_WORKERS)]
            for future in futures:
                future.result(timeout=DEADLINE_GUARD_OPERATION_TIMEOUT_S)
    assert time.monotonic() - started < DEADLINE_GUARD_OPERATION_TIMEOUT_S
    assert config.ledger_connection_count() == baseline


def test_ledger_query_deadline_budget_refuses():
    seed_deadline_guard_ledger()
    try:
        with advance_deadline_guard_on_sqlite_progress():
            try:
                budget.exceeded(deadline_s=DEADLINE_GUARD_BUDGET_S)
            except ledger_read.LedgerQueryDeadline:
                pass
            else:
                raise AssertionError("deadline must raise, never manufacture an infinite cap")
    finally:
        close_deadline_guard_thread_ledger()


def test_ledger_query_deadline_observer_setup_degrades_to_heap():
    seed_deadline_guard_ledger()
    close_deadline_guard_thread_ledger()
    baseline = config.ledger_connection_count()
    original_read_owner = config.managed_ledger_read_connection
    observer_failures = [TimeoutError(DEADLINE_GUARD_FAILURE_MESSAGE),
                         ledger_read.LedgerReadTimeout(DEADLINE_GUARD_BUDGET_S)]
    for sqlite_code in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED,
                        sqlite3.SQLITE_INTERRUPT, sqlite3.SQLITE_CORRUPT, sqlite3.SQLITE_ERROR):
        sqlite_failure = sqlite3.OperationalError(DEADLINE_GUARD_FAILURE_MESSAGE)
        sqlite_failure.sqlite_errorcode = sqlite_code
        observer_failures.append(sqlite_failure)
    for observer_failure, exhaust_budget, failure_stage in itertools.product(
            observer_failures, (False, True), (DEADLINE_GUARD_BEFORE_OBSERVER_OPEN,
                DEADLINE_GUARD_ENTERED_OBSERVER_OWNER, DEADLINE_GUARD_OBSERVER_GENERATION_READ)):
        opened_owners = []
        fatal_observer_failure = getattr(observer_failure, "sqlite_errorcode", None) in (
            sqlite3.SQLITE_CORRUPT, sqlite3.SQLITE_ERROR)

        @contextmanager
        def inject_deadline_guard_observer_open(*args, owners=opened_owners,
                                               exhaust=exhaust_budget, stage=failure_stage,
                                               failure=observer_failure, **kwargs):
            owners.append(args)
            is_observer = len(owners) == DEADLINE_GUARD_OBSERVER_OWNER_COUNT
            if is_observer and exhaust:
                request = ledger_read._LEDGER_ACTIVE_REQUEST_SCOPE.get()
                request.deadline_at = time.monotonic()
            if is_observer and stage == DEADLINE_GUARD_BEFORE_OBSERVER_OPEN:
                raise failure
            with original_read_owner(*args, **kwargs) as connection:
                if is_observer:
                    if stage == DEADLINE_GUARD_OBSERVER_GENERATION_READ:
                        proxy = Mock(wraps=connection)
                        proxy.execute.side_effect = failure
                        yield proxy
                        return
                    raise failure
                yield connection

        try:
            with patch.object(config, "managed_ledger_read_connection",
                              side_effect=inject_deadline_guard_observer_open):
                try:
                    result = budget.spent_since(DEADLINE_GUARD_DAY)
                except ledger_read.LedgerQueryDeadline:
                    assert exhaust_budget
                except ledger_read.LedgerReadError:
                    assert fatal_observer_failure
                else:
                    assert not exhaust_budget and not fatal_observer_failure
                    assert result == DEADLINE_GUARD_ROWS
            assert len(opened_owners) == DEADLINE_GUARD_OBSERVER_OWNER_COUNT
            assert getattr(budget._LEDGER_TL, "projection_observer", None) is None
            assert config.ledger_connection_count() == baseline
        finally:
            close_deadline_guard_thread_ledger()



def test_ledger_query_deadline_preserves_pending_write():
    current = seed_deadline_guard_ledger()
    connection = current._conn
    try:
        current._defer = True
        record_deadline_guard_fixture_event(current, DEADLINE_GUARD_PENDING_ID, DEADLINE_GUARD_PENDING_AMOUNT)
        current._defer = False
        pending_rows = capture_deadline_guard_rows(current)
        audit_before = tuple(tuple(row) for row in connection.execute(DEADLINE_GUARD_AUDIT_SQL))
        assert connection.in_transaction
        with patch.object(ledger_writer, "current_ledger_writer_connection", return_value=connection):
            assert budget.spent_since(DEADLINE_GUARD_MONTH) == DEADLINE_GUARD_ROWS
            assert budget.spent_all_time() == DEADLINE_GUARD_ROWS
            verdict = budget.exceeded(0.0)
            assert verdict is None or verdict[0] != "ledger-query-deadline"
            with advance_deadline_guard_on_sqlite_progress():
                try:
                    budget.spent_since(DEADLINE_GUARD_DAY, deadline_s=DEADLINE_GUARD_BUDGET_S)
                except ledger_read.LedgerQueryDeadline:
                    pass
                else:
                    raise AssertionError("snapshot aggregate must still enforce its deadline")
        assert current._conn is connection and connection.in_transaction
        assert capture_deadline_guard_rows(current) == pending_rows
        assert tuple(tuple(row) for row in connection.execute(DEADLINE_GUARD_AUDIT_SQL)) == audit_before
        connection.commit()
        assert capture_deadline_guard_rows(current) == pending_rows
        connection.execute(DEADLINE_GUARD_DELETE_PENDING_SQL, (DEADLINE_GUARD_PENDING_ID,))
        connection.commit()
    finally:
        close_deadline_guard_thread_ledger()



def test_deferred_writer_deadline_guard_counts_pending_spend():
    current = seed_deadline_guard_ledger()
    connection = current._conn
    try:
        current._defer = True
        record_deadline_guard_fixture_event(current, DEADLINE_GUARD_PENDING_ID, DEADLINE_GUARD_PENDING_AMOUNT)
        current._defer = False
        connection._ledger_bulk_commit_deferred = True
        with patch.object(ledger_writer, "current_ledger_writer_connection", return_value=connection):
            assert budget.spent_since(DEADLINE_GUARD_DAY) == (
                DEADLINE_GUARD_ROWS + int(DEADLINE_GUARD_PENDING_AMOUNT))
        assert connection.in_transaction
    finally:
        connection._ledger_bulk_commit_deferred = False
        connection.rollback()
        close_deadline_guard_thread_ledger()


def test_ledger_query_deadline_replacement_fault_recovers():
    for failure_mode in ("open", "aggregate-registration"):
        current = seed_deadline_guard_ledger()
        budget.spent_all_time()  # include the managed read-only generation observer in the live baseline
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
            with advance_deadline_guard_on_sqlite_progress(), \
                    patch.object(config, "open_ledger_connection", side_effect=fail_deadline_guard_replacement):
                try:
                    budget.spent_since(DEADLINE_GUARD_DAY, deadline_s=DEADLINE_GUARD_BUDGET_S)
                except ledger_read.LedgerReadError:
                    pass
                else:
                    raise AssertionError("replacement failure must remain a typed refusal")
                try:
                    budget.exceeded(deadline_s=DEADLINE_GUARD_BUDGET_S)
                except ledger_read.LedgerReadError:
                    pass
                else:
                    raise AssertionError("unavailable snapshot must refuse")
            assert config.ledger_connection_count() == baseline
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
        with advance_deadline_guard_on_sqlite_progress() as scan_clock:
            started = scan_clock.monotonic()
            with ledger_read.ledger_request_scope(
                    deadline_s=DEADLINE_GUARD_OPERATION_TIMEOUT_S):
                assert budget.spent_since(
                    DEADLINE_GUARD_DAY,
                    deadline_s=DEADLINE_GUARD_OPERATION_TIMEOUT_S,
                ) == DEADLINE_GUARD_ROWS
            scan_duration = scan_clock.monotonic() - started
            shared_budget = scan_duration * DEADLINE_GUARD_SHARED_SCAN_MULTIPLIER
            started = time.monotonic()
            try:
                budget.exceeded(deadline_s=shared_budget)
            except ledger_read.LedgerQueryDeadline:
                pass
            else:
                raise AssertionError("daily and monthly scans must share one caller deadline")
            elapsed = time.monotonic() - started
        assert elapsed < shared_budget + DEADLINE_GUARD_OPERATION_TIMEOUT_S
    finally:
        close_deadline_guard_thread_ledger()


if __name__ == "__main__":
    sys.exit(run_pytest_style(globals()))
