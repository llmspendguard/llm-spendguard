"""Load regression for production-posture SQLite writes on a disposable shared database."""
import math
import multiprocessing
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import time

from _pytest_fixture_bridge import align_test_startup_home, run_pytest_style

align_test_startup_home(prefix=__name__)

from spendguard import config

SQLITE_LOAD_DATABASE_FILENAME = "cross-process-load.db"
SQLITE_LOAD_WRITER_LEVELS = (2, 4, 8, 16, config.CODEX_DAEMON_MAX_WAITERS_DEFAULT)
SQLITE_LOAD_WRITES_PER_PROCESS = 64
SQLITE_LOAD_PROCESS_JOIN_GRACE_S = config.LEDGER_WRITE_TIMEOUT_S
SQLITE_LOAD_READY_TIMEOUT_S = config.LEDGER_WRITE_TIMEOUT_S
SQLITE_LOAD_RESULT_TIMEOUT_S = config.LEDGER_WRITE_TIMEOUT_S
SQLITE_LOAD_PERCENTILE = 99
SQLITE_LOAD_MILLISECONDS_PER_SECOND = 1000
SQLITE_LOAD_EVENT_DDL = """CREATE TABLE load_events (
    event_id TEXT PRIMARY KEY, writer_id INTEGER NOT NULL, sequence_id INTEGER NOT NULL,
    recorded_monotonic REAL NOT NULL)"""
SQLITE_LOAD_AUDIT_DDL = """CREATE TABLE load_audit (
    audit_id INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT NOT NULL, action TEXT NOT NULL)"""
SQLITE_LOAD_EVENT_INSERT = (
    "INSERT INTO load_events(event_id,writer_id,sequence_id,recorded_monotonic) VALUES(?,?,?,?)")
SQLITE_LOAD_AUDIT_INSERT = "INSERT INTO load_audit(event_id,action) VALUES(?,?)"
SQLITE_LOAD_BEGIN_WRITE = "BEGIN IMMEDIATE"
SQLITE_LOAD_AUDIT_ACTION = "ingest"


def sqlite_load_percentile(values, percentile):
    ordered_values = sorted(values)
    rank = max(0, math.ceil((percentile / 100) * len(ordered_values)) - 1)
    return ordered_values[rank]


def run_sqlite_load_writer(database_path, load_run_id, writer_id, start_event, ready_queue, result_queue):
    connection = config.open_ledger_connection(path=database_path, timeout=config.LEDGER_WRITE_TIMEOUT_S)
    latencies = []
    lock_holds = []
    busy_errors = []
    locked_errors = []
    other_errors = []
    try:
        ready_queue.put(writer_id)
        assert start_event.wait(SQLITE_LOAD_READY_TIMEOUT_S), "load coordinator did not release writer cohort"
        for sequence_id in range(SQLITE_LOAD_WRITES_PER_PROCESS):
            event_id = f"{load_run_id}:{writer_id}:{sequence_id}"
            write_started = time.perf_counter()
            try:
                connection.execute(SQLITE_LOAD_BEGIN_WRITE)
                lock_acquired = time.perf_counter()
                connection.execute(
                    SQLITE_LOAD_EVENT_INSERT, (event_id, writer_id, sequence_id, time.monotonic()))
                connection.execute(SQLITE_LOAD_AUDIT_INSERT, (event_id, SQLITE_LOAD_AUDIT_ACTION))
                connection.commit()
                committed = time.perf_counter()
                latencies.append(committed - write_started)
                lock_holds.append(committed - lock_acquired)
            except sqlite3.Error as error:
                connection.rollback()
                if getattr(error, "sqlite_errorcode", None) == sqlite3.SQLITE_BUSY:
                    busy_errors.append(str(error))
                elif getattr(error, "sqlite_errorcode", None) == sqlite3.SQLITE_LOCKED:
                    locked_errors.append(str(error))
                else:
                    other_errors.append(f"{type(error).__name__}: {error}")
    finally:
        busy_timeout_ms = connection.execute("PRAGMA busy_timeout").fetchone()[0]
        connection.close()
    result_queue.put((latencies, lock_holds, busy_errors, locked_errors, other_errors, busy_timeout_ms))


def measure_sqlite_cross_process_load(database_path, writer_count):
    process_context = multiprocessing.get_context("spawn")
    start_event = process_context.Event()
    ready_queue = process_context.Queue()
    result_queue = process_context.Queue()
    processes = [process_context.Process(
        target=run_sqlite_load_writer,
        args=(database_path, writer_count, writer_id, start_event, ready_queue, result_queue),
    ) for writer_id in range(writer_count)]
    for process in processes:
        process.start()
    for _ in processes:
        ready_queue.get(timeout=SQLITE_LOAD_READY_TIMEOUT_S)
    start_event.set()
    results = [result_queue.get(timeout=SQLITE_LOAD_RESULT_TIMEOUT_S) for _ in processes]
    for process in processes:
        process.join(timeout=SQLITE_LOAD_PROCESS_JOIN_GRACE_S)
        assert process.exitcode == 0, f"SQLite load writer exited with {process.exitcode}"
    latencies = [latency for result in results for latency in result[0]]
    lock_holds = [lock_hold for result in results for lock_hold in result[1]]
    busy_errors = [error for result in results for error in result[2]]
    locked_errors = [error for result in results for error in result[3]]
    other_errors = [error for result in results for error in result[4]]
    busy_timeouts_ms = {result[5] for result in results}
    assert len(busy_timeouts_ms) == 1
    return {
        "writers": writer_count,
        "writes": len(latencies),
        "busy_errors": len(busy_errors),
        "locked_errors": len(locked_errors),
        "other_errors": other_errors,
        "p50_ms": sqlite_load_percentile(latencies, 50) * SQLITE_LOAD_MILLISECONDS_PER_SECOND,
        "p99_ms": sqlite_load_percentile(latencies, SQLITE_LOAD_PERCENTILE) * SQLITE_LOAD_MILLISECONDS_PER_SECOND,
        "max_ms": max(latencies) * SQLITE_LOAD_MILLISECONDS_PER_SECOND,
        "max_lock_hold_ms": max(lock_holds) * SQLITE_LOAD_MILLISECONDS_PER_SECOND,
        "busy_timeout_ms": busy_timeouts_ms.pop(),
    }


def test_sqlite_cross_process_write_load():
    with tempfile.TemporaryDirectory(dir=config.HOME) as load_directory:
        database_path = os.fspath(Path(load_directory) / SQLITE_LOAD_DATABASE_FILENAME)
        with config.open_ledger_connection(path=database_path) as setup_connection:
            assert setup_connection.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
            assert setup_connection.execute("PRAGMA synchronous").fetchone()[0] == 1
            assert setup_connection.execute("PRAGMA busy_timeout").fetchone()[0] == config.LEDGER_BUSY_TIMEOUT_MS
            setup_connection.execute(SQLITE_LOAD_EVENT_DDL)
            setup_connection.execute(SQLITE_LOAD_AUDIT_DDL)
        measurements = [
            measure_sqlite_cross_process_load(database_path, writer_count)
            for writer_count in SQLITE_LOAD_WRITER_LEVELS
        ]
        expected_writes = SQLITE_LOAD_WRITES_PER_PROCESS * sum(SQLITE_LOAD_WRITER_LEVELS)
        with config.open_ledger_connection(path=database_path) as verification_connection:
            actual_events = verification_connection.execute("SELECT COUNT(*) FROM load_events").fetchone()[0]
            actual_audits = verification_connection.execute("SELECT COUNT(*) FROM load_audit").fetchone()[0]
        assert actual_events == expected_writes
        assert actual_audits == expected_writes
        for measurement in measurements:
            print("SQLITE_LOAD", measurement)
            assert measurement["busy_errors"] == 0
            assert measurement["locked_errors"] == 0
            assert not measurement["other_errors"]
            assert measurement["writes"] == SQLITE_LOAD_WRITES_PER_PROCESS * measurement["writers"]
            assert measurement["max_lock_hold_ms"] < measurement["busy_timeout_ms"]


if __name__ == "__main__":
    sys.exit(run_pytest_style(globals()))
