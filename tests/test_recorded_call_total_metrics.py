"""Isolated, offline guards for raw call totals and process read accounting."""
import os
import tempfile
import concurrent.futures
import threading
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("SPENDGUARD_HOME", tempfile.mkdtemp())

import spendguard
from spendguard import config, ledger_read

RECORDED_GUARD_DATABASE = "consumer-ledger.sqlite"
RECORDED_GUARD_ABSENT = "absent.sqlite"
RECORDED_GUARD_CORRUPT = "corrupt.sqlite"
RECORDED_GUARD_EMPTY = "empty.sqlite"
RECORDED_GUARD_COSTS = (0.25, 1.5, -0.125)
RECORDED_GUARD_LOCK_BUDGET_S = 0.05
RECORDED_GUARD_CORRUPT_BYTES = b"this is deliberately not a SQLite database"
RECORDED_GUARD_CONNECTION_SENTINEL = 29
RECORDED_GUARD_READ_INCREMENT = 1
RECORDED_GUARD_INVALID_BUDGETS = (float("nan"), float("inf"), 0.0, -1.0)
RECORDED_GUARD_ADMISSION_BOUND_S = 1.0
RECORDED_GUARD_ADMISSION_WORKERS = 2
RECORDED_GUARD_DISABLED_PROGRESS_INTERVAL = 0


def assert_recorded_admission_deadline(path):
    admission_held = threading.Event()
    release_admission = threading.Event()

    def hold_consumer_admission_lock():
        with config._LEDGER_ADMISSION.lock:
            admission_held.set()
            release_admission.wait()

    with concurrent.futures.ThreadPoolExecutor(max_workers=RECORDED_GUARD_ADMISSION_WORKERS) as workers:
        holder = workers.submit(hold_consumer_admission_lock)
        try:
            assert admission_held.wait(RECORDED_GUARD_ADMISSION_BOUND_S)
            reader = workers.submit(ledger_read.read_recorded_call_total, path,
                                    time_budget=RECORDED_GUARD_LOCK_BUDGET_S)
            try:
                reader.result(timeout=RECORDED_GUARD_ADMISSION_BOUND_S)
            except ledger_read.LedgerReadTimeout:
                pass
            except concurrent.futures.TimeoutError as error:
                raise AssertionError("admission wait escaped the caller deadline") from error
            else:
                raise AssertionError("blocked admission must raise typed timeout")
        finally:
            release_admission.set()
            holder.result(timeout=RECORDED_GUARD_ADMISSION_BOUND_S)


def assert_recorded_unknown(path):
    try:
        ledger_read.read_recorded_call_total(path, time_budget=RECORDED_GUARD_LOCK_BUDGET_S)
    except ledger_read.LedgerReadError:
        pass
    else:
        raise AssertionError("unmeasurable raw total must raise typed error, never return zero")


def recorded_tolerant_consumer(path):
    try:
        return ledger_read.read_recorded_call_total(path)
    except ledger_read.LedgerReadError:
        return 0.0


def recorded_fail_closed_consumer(path):
    try:
        return ledger_read.read_recorded_call_total(path)
    except ledger_read.LedgerReadError:
        return None


def run_recorded_total_metrics_guards():
    spendguard.require()
    isolated_metrics = ledger_read._LedgerReadProcessMetrics()
    untouched_metrics = ledger_read._LedgerReadProcessMetrics()
    with isolated_metrics.observe_ledger_read_attempt():
        pass
    assert isolated_metrics.snapshot_ledger_read_process_metrics()["read_calls_issued"] == RECORDED_GUARD_READ_INCREMENT
    assert untouched_metrics.snapshot_ledger_read_process_metrics()["read_calls_issued"] == 0
    with tempfile.TemporaryDirectory() as directory:
        home = Path(directory)
        database = home / RECORDED_GUARD_DATABASE
        writer = config.open_ledger_connection(database, tune=False)
        try:
            writer.execute("CREATE TABLE calls(cost REAL)")
            writer.executemany("INSERT INTO calls(cost) VALUES (?)", ((cost,) for cost in RECORDED_GUARD_COSTS))
            writer.commit()
            before = ledger_read.ledger_read_metrics()
            assert ledger_read.read_recorded_call_total(database) == sum(RECORDED_GUARD_COSTS)
            after = ledger_read.ledger_read_metrics()
            assert after["read_calls_issued"] == before["read_calls_issued"] + RECORDED_GUARD_READ_INCREMENT
            assert after["cumulative_read_seconds"] >= before["cumulative_read_seconds"]
            assert after["open_connections"] == before["open_connections"]
            with patch.object(config, "open_ledger_connection",
                              side_effect=config.LedgerConnectionLimitError("test resource refusal")):
                assert_recorded_unknown(database)
            with patch.object(ledger_read, "DEFAULT_LEDGER_READ_PROGRESS_VM_INSTRUCTIONS",
                              RECORDED_GUARD_DISABLED_PROGRESS_INTERVAL):
                assert_recorded_unknown(database)
            for invalid_budget in RECORDED_GUARD_INVALID_BUDGETS:
                try:
                    ledger_read.read_recorded_call_total(database, time_budget=invalid_budget)
                except ValueError:
                    pass
                else:
                    raise AssertionError("invalid read budget must refuse rather than disable its bound")
            writer.execute("BEGIN EXCLUSIVE")
            assert_recorded_unknown(database)
            writer.rollback()
            assert ledger_read.read_recorded_call_total(database) == sum(RECORDED_GUARD_COSTS)
        finally:
            writer.close()
        assert_recorded_admission_deadline(database)
        # Reproduce cyclic-GC finalization inside a bounded open while another
        # caller owns admission. An explicitly closed slot must never wait again.
        original_recorded_guard_open = config.open_ledger_connection

        def finalize_closed_recorded_handle_before_admission(*args, **kwargs):
            writer.__del__()
            return original_recorded_guard_open(*args, **kwargs)

        before_finalizer_count = config.ledger_connection_count()
        with patch.object(config, "open_ledger_connection",
                          side_effect=finalize_closed_recorded_handle_before_admission):
            assert_recorded_admission_deadline(database)
        assert config.ledger_connection_count() == before_finalizer_count
        unclosed_recorded_guard = config.open_ledger_connection(database, tune=False)
        assert config.ledger_connection_count() == before_finalizer_count + RECORDED_GUARD_READ_INCREMENT
        unclosed_recorded_guard.__del__()
        assert config.ledger_connection_count() == before_finalizer_count
        unclosed_recorded_guard.__del__()
        assert config.ledger_connection_count() == before_finalizer_count
        missing = home / RECORDED_GUARD_ABSENT
        assert_recorded_unknown(missing)
        assert not missing.exists()
        assert recorded_tolerant_consumer(missing) == 0.0
        assert recorded_fail_closed_consumer(missing) is None
        corrupt = home / RECORDED_GUARD_CORRUPT
        corrupt.write_bytes(RECORDED_GUARD_CORRUPT_BYTES)
        assert_recorded_unknown(corrupt)
        assert corrupt.read_bytes() == RECORDED_GUARD_CORRUPT_BYTES
        empty = home / RECORDED_GUARD_EMPTY
        config.open_ledger_connection(empty, tune=False).close()
        assert_recorded_unknown(empty)
        with patch.object(config, "ledger_connection_count", return_value=RECORDED_GUARD_CONNECTION_SENTINEL) as counter:
            assert ledger_read.ledger_read_metrics()["open_connections"] == RECORDED_GUARD_CONNECTION_SENTINEL
            counter.assert_called_once_with()
        assert ledger_read.ledger_read_metrics()["cumulative_read_seconds"] >= after["cumulative_read_seconds"]
    print("recorded total and read metrics guards passed")


if __name__ == "__main__":
    run_recorded_total_metrics_guards()
