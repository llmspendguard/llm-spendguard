"""Offline operation-owned connection regression; retained handles expose real leaks."""
import os
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
    lease_key = run_recycler_lease_ownership_probe.__name__
    retained = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as recycler:
        try:
            with config.ledger_op(lease_key, ensure_lifecycle_soak_schema, ledger_path) as outer:
                retained.append(outer)
                recycler.submit(config.recycle_idle_ledger_connections).result()
                assert not outer._ledger_released
                cursor = outer.execute("SELECT value FROM lifecycle_soak ORDER BY value")
                recycler.submit(config.recycle_idle_ledger_connections).result()
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
    run_operation_owned_lifecycle_soak()
