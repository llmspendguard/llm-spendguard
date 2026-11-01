"""Offline operation-owned connection regression; retained handles expose real leaks."""
import os
import faulthandler
import subprocess
import sys
import tempfile
import concurrent.futures
import threading
import time
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("SPENDGUARD_TEST_ISOLATED", "1")
os.environ.setdefault("SPENDGUARD_HOME", tempfile.mkdtemp())

from spendguard import config

LIFECYCLE_SOAK_OPERATIONS = 100
LIFECYCLE_SOAK_SCHEMA = "CREATE TABLE IF NOT EXISTS lifecycle_soak(value INTEGER)"
LIFECYCLE_SOAK_POOL_KEY = "lifecycle-soak"
LIFECYCLE_READ_TEST_CAP = 2
LIFECYCLE_READ_TEST_WORKERS = 6
LIFECYCLE_READ_HOLD_S = 0.01
RECYCLER_DEADLOCK_WATCHDOG_MULTIPLIER = 100
RECYCLER_DEADLOCK_WATCHDOG_S = LIFECYCLE_READ_HOLD_S * RECYCLER_DEADLOCK_WATCHDOG_MULTIPLIER
RECYCLER_FAULTHANDLER_GRACE_MULTIPLIER = 2
RECYCLER_FAULTHANDLER_WATCHDOG_S = (
    RECYCLER_DEADLOCK_WATCHDOG_S * RECYCLER_FAULTHANDLER_GRACE_MULTIPLIER
)
RECYCLER_SUBPROCESS_TIMEOUT_MULTIPLIER = 3
RECYCLER_SUBPROCESS_TIMEOUT_S = RECYCLER_DEADLOCK_WATCHDOG_S * RECYCLER_SUBPROCESS_TIMEOUT_MULTIPLIER
RECYCLER_CHILD_MODE = "--recycler-lease-child"
RECYCLER_CHILD_INJECT_LOCK_REGRESSION = "--inject-recycler-lock-regression"
RECYCLER_DEADLOCK_EXIT_CODE = 86
RECYCLER_DEADLOCK_DIAGNOSTIC = (
    "recycler-lock regression: recycle_idle_ledger_connections did not return against a held lease "
    f"within {RECYCLER_DEADLOCK_WATCHDOG_S:g}s"
)


def ensure_lifecycle_soak_schema(connection):
    connection.execute(LIFECYCLE_SOAK_SCHEMA)


def run_operation_owned_lifecycle_soak():
    retained_operation_handles = []
    with tempfile.TemporaryDirectory() as directory:
        ledger_path = Path(directory) / "soak.sqlite"
        with config.ledger_op(LIFECYCLE_SOAK_POOL_KEY, ensure_lifecycle_soak_schema, ledger_path):
            pass
        baseline = config.ledger_connection_count()
        for operation in range(LIFECYCLE_SOAK_OPERATIONS):
            with config.ledger_op(f"{LIFECYCLE_SOAK_POOL_KEY}-{operation}",
                                  ensure_lifecycle_soak_schema, ledger_path) as connection:
                connection.execute("INSERT INTO lifecycle_soak VALUES (?)", (operation,))
                assert connection.execute("SELECT COUNT(*) FROM lifecycle_soak").fetchone()[0] == operation + 1
                retained_operation_handles.append(connection)
        live = config.ledger_connection_count() - baseline
        print(f"operation-owned soak: operations={LIFECYCLE_SOAK_OPERATIONS}, live growth={live}")
        assert live == 0, f"operation returns leaked {live} live SQLite connections"
        for operation in range(LIFECYCLE_SOAK_OPERATIONS):
            try:
                with config.ledger_op(LIFECYCLE_SOAK_POOL_KEY, ensure_lifecycle_soak_schema, ledger_path):
                    raise RuntimeError(LIFECYCLE_SOAK_POOL_KEY)
            except RuntimeError:
                pass
        assert config.ledger_connection_count() == baseline
        assert run_bounded_read_lifecycle_probe(ledger_path, baseline) <= LIFECYCLE_READ_TEST_CAP
        run_legacy_pool_cap_probe(ledger_path, baseline)
        run_recycler_lease_ownership_probe(ledger_path, baseline)
        from spendguard.ledger_writer import close_ledger_writers
        close_ledger_writers()
        run_spend_ledger_instance_soak(Path(directory) / "instances.sqlite", config.ledger_connection_count())


def run_spend_ledger_instance_soak(ledger_path, baseline):
    from spendguard.ledger import SpendLedger
    from spendguard.ledger_writer import close_ledger_writers
    retained_ledgers = []
    for _ in range(LIFECYCLE_SOAK_OPERATIONS):
        ledger = SpendLedger(ledger_path)
        retained_ledgers.append(ledger)
        assert ledger.spent_dec() == "0"
        assert ledger._conn is None
        assert config.ledger_connection_count() - baseline <= 1
    close_ledger_writers()
    assert config.ledger_connection_count() == baseline
    print(f"SpendLedger instance soak: operations={LIFECYCLE_SOAK_OPERATIONS}, owner peak=1, returned=0")


def run_legacy_pool_cap_probe(ledger_path, baseline):
    with patch.object(config, "ledger_read_pool_settings", return_value=(
            LIFECYCLE_READ_TEST_CAP, config.LEDGER_POOL_IDLE_TIMEOUT_DEFAULT_S)):
        retained = []
        for operation in range(LIFECYCLE_SOAK_OPERATIONS):
            with config.ledger_op(
                f"{LIFECYCLE_SOAK_POOL_KEY}-retained-{operation}",
                ensure_lifecycle_soak_schema, ledger_path) as connection:
                retained.append(connection)
                config.recycle_idle_ledger_connections()
                assert not connection._ledger_released
                assert connection.execute("SELECT COUNT(*) FROM lifecycle_soak").fetchone()[0] == LIFECYCLE_SOAK_OPERATIONS
                assert config.ledger_connection_count() - baseline <= LIFECYCLE_READ_TEST_CAP
        config.recycle_idle_ledger_connections()
        assert config.ledger_connection_count() == baseline
        assert all(connection._ledger_released for connection in retained)
    print(f"legacy pool soak: operations={LIFECYCLE_SOAK_OPERATIONS}, cap={LIFECYCLE_READ_TEST_CAP}, returned=0")


def run_recycler_lease_ownership_probe(ledger_path, baseline):
    del baseline  # Connection counts are process-local; each isolated child establishes its own baseline.
    child_environment = os.environ.copy()
    child_environment["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
    child_environment["SPENDGUARD_TEST_ISOLATED"] = "1"
    with tempfile.TemporaryDirectory() as isolated_home:
        child_environment["SPENDGUARD_HOME"] = isolated_home
        control = subprocess.run(
            [sys.executable, __file__, RECYCLER_CHILD_MODE, os.fspath(ledger_path)],
            env=child_environment,
            capture_output=True,
            text=True,
            timeout=RECYCLER_SUBPROCESS_TIMEOUT_S,
            check=False,
        )
        assert control.returncode == 0, control.stdout + control.stderr

        regression_started = time.monotonic()
        injected_regression = subprocess.run(
            [sys.executable, __file__, RECYCLER_CHILD_MODE, os.fspath(ledger_path),
             RECYCLER_CHILD_INJECT_LOCK_REGRESSION],
            env=child_environment,
            capture_output=True,
            text=True,
            timeout=RECYCLER_SUBPROCESS_TIMEOUT_S,
            check=False,
        )
        regression_elapsed = time.monotonic() - regression_started
        assert injected_regression.returncode != 0, "injected recycler-lock regression unexpectedly passed"
        assert RECYCLER_DEADLOCK_DIAGNOSTIC in injected_regression.stderr, injected_regression.stderr
        assert regression_elapsed < RECYCLER_SUBPROCESS_TIMEOUT_S


def run_isolated_recycler_lease_ownership_probe(ledger_path, inject_lock_regression):
    lease_key = run_recycler_lease_ownership_probe.__name__
    retained = []
    with config.ledger_op(LIFECYCLE_SOAK_POOL_KEY, ensure_lifecycle_soak_schema, ledger_path):
        pass
    baseline = config.ledger_connection_count()
    recycler = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    if inject_lock_regression:
        recycler_lock_regression = threading.Event()

        def recycle_idle_ledger_connections_lock_regression():
            recycler_lock_regression.wait()

        recycler_operation = recycle_idle_ledger_connections_lock_regression
    else:
        recycler_operation = config.recycle_idle_ledger_connections
    try:
        try:
            with config.ledger_op(lease_key, ensure_lifecycle_soak_schema, ledger_path) as outer:
                retained.append(outer)
                recycler.submit(recycler_operation).result(timeout=RECYCLER_DEADLOCK_WATCHDOG_S)
                assert not outer._ledger_released
                cursor = outer.execute("SELECT value FROM lifecycle_soak ORDER BY value")
                recycler.submit(recycler_operation).result(timeout=RECYCLER_DEADLOCK_WATCHDOG_S)
                assert cursor.fetchone()[0] == 0
                with config.ledger_op(lease_key, ensure_lifecycle_soak_schema, ledger_path) as inner:
                    assert inner is outer
                    assert not inner._ledger_released
                assert not outer._ledger_released
                assert cursor.fetchone()[0] == 1
                with config.ledger_op(lease_key, ensure_lifecycle_soak_schema, ledger_path) as inner:
                    assert inner is outer
                    raise RuntimeError(lease_key)
        except RuntimeError as error:
            assert str(error) == lease_key
        else:
            raise AssertionError("nested lease exception was swallowed")
    except concurrent.futures.TimeoutError:
        faulthandler.dump_traceback()
        os.write(2, f"{RECYCLER_DEADLOCK_DIAGNOSTIC}\n".encode())
        recycler.shutdown(wait=False, cancel_futures=True)
        os._exit(RECYCLER_DEADLOCK_EXIT_CODE)
    else:
        recycler.shutdown(wait=True)
    assert all(connection._ledger_released for connection in retained)
    assert config.ledger_connection_count() == baseline

    raw_keys = [f"{lease_key}-{index}" for index in range(LIFECYCLE_READ_TEST_CAP)]
    try:
        with patch.object(config, "ledger_read_pool_settings", return_value=(
                LIFECYCLE_READ_TEST_CAP, LIFECYCLE_READ_HOLD_S)):
            raw_handles = [config.pooled_ledger_conn(key, ensure_lifecycle_soak_schema, ledger_path)
                           for key in raw_keys]
            config.recycle_idle_ledger_connections()
            assert all(not connection._ledger_released for connection in raw_handles)
            assert config.ledger_connection_count() == baseline + LIFECYCLE_READ_TEST_CAP
            try:
                config.pooled_ledger_conn(lease_key, ensure_lifecycle_soak_schema, ledger_path)
            except config.LedgerConnectionLimitError:
                pass
            else:
                raise AssertionError("borrowed raw handles bypassed hard read cap")
    finally:
        for key in raw_keys:
            config.reset_ledger_conn(key)
    assert config.ledger_connection_count() == baseline
    print("recycler regression: pre-execute, mid-cursor, nested exception, borrowed raw cap passed")


def run_recycler_lease_child_mode(ledger_path, inject_lock_regression):
    faulthandler.enable()
    # The Future bound fires first; this second layer still dumps stacks if timeout handling itself wedges.
    faulthandler.dump_traceback_later(RECYCLER_FAULTHANDLER_WATCHDOG_S, exit=True)
    try:
        run_isolated_recycler_lease_ownership_probe(ledger_path, inject_lock_regression)
    finally:
        faulthandler.cancel_dump_traceback_later()


def run_bounded_read_lifecycle_probe(ledger_path, baseline):
    retained_read_handles = []
    read_lock = threading.Lock()
    observed_peak = baseline

    def hold_bounded_read_lease(operation):
        nonlocal observed_peak
        try:
            with config.managed_ledger_read_connection(ledger_path) as connection:
                with read_lock:
                    retained_read_handles.append(connection)
                    observed_peak = max(observed_peak, config.ledger_connection_count())
                assert connection.execute("SELECT COUNT(*) FROM lifecycle_soak").fetchone()[0] == LIFECYCLE_SOAK_OPERATIONS
                time.sleep(LIFECYCLE_READ_HOLD_S)
                raise RuntimeError(operation)
        except RuntimeError:
            pass

    with patch.object(config, "ledger_read_pool_settings", return_value=(
            LIFECYCLE_READ_TEST_CAP, config.LEDGER_POOL_IDLE_TIMEOUT_DEFAULT_S)):
        with concurrent.futures.ThreadPoolExecutor(max_workers=LIFECYCLE_READ_TEST_WORKERS) as readers:
            list(readers.map(hold_bounded_read_lease, range(LIFECYCLE_SOAK_OPERATIONS)))
    assert config.ledger_connection_count() == baseline
    print(f"read lease soak: operations={LIFECYCLE_SOAK_OPERATIONS}, peak={observed_peak-baseline}, returned=0")
    return observed_peak - baseline


if __name__ == "__main__":
    if len(sys.argv) >= 3 and sys.argv[1] == RECYCLER_CHILD_MODE:
        run_recycler_lease_child_mode(
            Path(sys.argv[2]),
            len(sys.argv) >= 4 and sys.argv[3] == RECYCLER_CHILD_INJECT_LOCK_REGRESSION,
        )
    else:
        run_operation_owned_lifecycle_soak()
