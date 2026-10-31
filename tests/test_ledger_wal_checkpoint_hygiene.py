"""Offline real-lock regression checks for bounded WAL hygiene."""
import pathlib
import sqlite3
import sys
import threading
import time

import pytest
from _pytest_fixture_bridge import align_test_startup_home, run_pytest_style

align_test_startup_home(prefix=__name__)

import spendguard
spendguard.require()
from spendguard import config, ledger_wal_maintenance as maintenance

WAL_HYGIENE_WAIT_MS = 200
WAL_HYGIENE_RELEASE_S = 0.04
WAL_HYGIENE_SCHEDULER_ALLOWANCE_S = 0.20
WAL_HYGIENE_THREAD_JOIN_S = 2.0
WAL_HYGIENE_BURST_BATCHES = 4
WAL_HYGIENE_BATCH_ROWS = 128
WAL_HYGIENE_AUTOCHECKPOINT_PAGES = 64
WAL_HYGIENE_AUTOCHECKPOINT_ENV = "SPENDGUARD_LEDGER_WAL_AUTOCHECKPOINT_PAGES"
WAL_HYGIENE_PAYLOAD_BYTES = 4096
WAL_HYGIENE_CREATE_SQL = "CREATE TABLE checkpoint_hygiene (payload BLOB)"
WAL_HYGIENE_INSERT_SQL = "INSERT INTO checkpoint_hygiene VALUES (?)"
WAL_HYGIENE_COUNT_SQL = "SELECT COUNT(*) FROM checkpoint_hygiene"
WAL_HYGIENE_MISSING_SQL = "SELECT * FROM missing_checkpoint_hygiene_table"
WAL_HYGIENE_SECONDS_PER_MS = 0.001
WAL_HYGIENE_MIN_WAIT_FRACTION = 0.5
WAL_HYGIENE_CHECKPOINT_WAIT_ENV = "SPENDGUARD_LEDGER_WAL_CHECKPOINT_BUSY_TIMEOUT_MS"
WAL_HYGIENE_READER_WAIT_ENV = "SPENDGUARD_LEDGER_READ_BUSY_TIMEOUT_MS"


def _open_wal_hygiene_writer(ledger_path):
    connection = sqlite3.connect(ledger_path, check_same_thread=False)
    config.tune_ledger_connection(connection)
    connection.execute(WAL_HYGIENE_CREATE_SQL)
    connection.execute(WAL_HYGIENE_INSERT_SQL, (bytes(WAL_HYGIENE_PAYLOAD_BYTES),))
    connection.commit()
    return connection


def check_wal_checkpoint_contention_no_longer_busies(tmp_path, monkeypatch):
    ledger_path = tmp_path / pathlib.Path(__file__).stem
    writer = _open_wal_hygiene_writer(ledger_path)
    monkeypatch.setenv(WAL_HYGIENE_CHECKPOINT_WAIT_ENV, str(WAL_HYGIENE_WAIT_MS))
    writer.execute("BEGIN IMMEDIATE")
    # Explicitly demonstrate the old zero-wait SQLite defect under this SAME lock.
    with sqlite3.connect(ledger_path, timeout=0) as old_probe:
        assert old_probe.execute(maintenance.WAL_CHECKPOINT_SQL).fetchone()[0] == 1
    released = threading.Event()

    def release_wal_hygiene_transient_writer():
        time.sleep(WAL_HYGIENE_RELEASE_S)
        writer.rollback()
        released.set()

    releaser = threading.Thread(target=release_wal_hygiene_transient_writer)
    releaser.start()
    try:
        report = maintenance.truncate_ledger_wal_bounded(ledger_path)
        assert report.status == maintenance.WAL_CHECKPOINT_SUCCESS, report
        assert released.is_set(), "checkpoint returned before transient lock release"
    finally:
        releaser.join(WAL_HYGIENE_THREAD_JOIN_S)
        writer.close()


def check_wal_checkpoint_stays_bounded(tmp_path, monkeypatch):
    ledger_path = tmp_path / pathlib.Path(__file__).stem
    writer = _open_wal_hygiene_writer(ledger_path)
    reader = sqlite3.connect(ledger_path)
    warnings = []
    monkeypatch.setenv(WAL_HYGIENE_CHECKPOINT_WAIT_ENV, str(WAL_HYGIENE_WAIT_MS))
    monkeypatch.setattr(maintenance._WAL_MAINTENANCE_LOG, "warning", lambda *args: warnings.append(args))
    try:
        reader.execute("BEGIN")
        reader.execute(WAL_HYGIENE_COUNT_SQL).fetchone()
        writer.execute(WAL_HYGIENE_INSERT_SQL, (bytes(WAL_HYGIENE_PAYLOAD_BYTES),))
        writer.commit()
        started = time.monotonic()
        report = maintenance.truncate_ledger_wal_bounded(ledger_path)
        elapsed = time.monotonic() - started
        configured_seconds = WAL_HYGIENE_WAIT_MS * WAL_HYGIENE_SECONDS_PER_MS
        assert report.status == maintenance.WAL_CHECKPOINT_BUSY, report
        assert warnings and warnings[-1][-1] == report
        assert elapsed >= configured_seconds * WAL_HYGIENE_MIN_WAIT_FRACTION, elapsed
        assert elapsed < configured_seconds + WAL_HYGIENE_SCHEDULER_ALLOWANCE_S, elapsed
    finally:
        reader.close()
        writer.close()


    _check_wal_hygiene_admission_wait_bound(ledger_path, monkeypatch)


def _check_wal_hygiene_admission_wait_bound(ledger_path, monkeypatch):
    admission_held = threading.Event()
    release_admission = threading.Event()
    warnings = []
    monkeypatch.setattr(maintenance._WAL_MAINTENANCE_LOG, "warning", lambda *args: warnings.append(args))

    def hold_wal_hygiene_admission_lock():
        with config._LEDGER_ADMISSION.lock:
            admission_held.set()
            release_admission.wait(WAL_HYGIENE_THREAD_JOIN_S)

    lock_holder = threading.Thread(target=hold_wal_hygiene_admission_lock)
    lock_holder.start()
    try:
        assert admission_held.wait(WAL_HYGIENE_THREAD_JOIN_S)
        started = time.monotonic()
        report = maintenance.truncate_ledger_wal_bounded(ledger_path)
        elapsed = time.monotonic() - started
        assert report.status == maintenance.WAL_CHECKPOINT_FAILED, report
        assert warnings and warnings[-1][-1] == report
        assert elapsed < (WAL_HYGIENE_WAIT_MS * WAL_HYGIENE_SECONDS_PER_MS
                          + WAL_HYGIENE_SCHEDULER_ALLOWANCE_S), elapsed
    finally:
        release_admission.set()
        lock_holder.join(WAL_HYGIENE_THREAD_JOIN_S)
    assert not lock_holder.is_alive()


def check_wal_checkpoint_wal_stays_bounded(tmp_path, monkeypatch):
    monkeypatch.setenv(WAL_HYGIENE_AUTOCHECKPOINT_ENV, str(WAL_HYGIENE_AUTOCHECKPOINT_PAGES))
    ledger_path = tmp_path / pathlib.Path(__file__).stem
    writer = _open_wal_hygiene_writer(ledger_path)
    try:
        for _ in range(WAL_HYGIENE_BURST_BATCHES):
            writer.executemany(WAL_HYGIENE_INSERT_SQL,
                               ((bytes(WAL_HYGIENE_PAYLOAD_BYTES),) for _ in range(WAL_HYGIENE_BATCH_ROWS)))
            writer.commit()
        report = maintenance.truncate_ledger_wal_bounded(ledger_path)
        assert report.status == maintenance.WAL_CHECKPOINT_SUCCESS, report
        assert report.log_frames == report.checkpointed_frames
        wal_bytes = pathlib.Path(str(ledger_path) + "-wal").stat().st_size
        assert wal_bytes <= config.LEDGER_JOURNAL_LIMIT_BYTES
        assert wal_bytes == 0
        with sqlite3.connect(ledger_path) as verifier:
            assert verifier.execute(WAL_HYGIENE_COUNT_SQL).fetchone()[0] == (
                1 + WAL_HYGIENE_BURST_BATCHES * WAL_HYGIENE_BATCH_ROWS)
        assert writer.execute("PRAGMA wal_autocheckpoint").fetchone()[0] == WAL_HYGIENE_AUTOCHECKPOINT_PAGES
    finally:
        writer.close()


def check_wal_checkpoint_reader_busy_timeout_applied(tmp_path, monkeypatch):
    ledger_path = tmp_path / pathlib.Path(__file__).stem
    writer = _open_wal_hygiene_writer(ledger_path)
    monkeypatch.setenv(WAL_HYGIENE_READER_WAIT_ENV, str(WAL_HYGIENE_WAIT_MS))
    try:
        with config.managed_ledger_read_connection(ledger_path) as reader:
            assert reader.execute("PRAGMA busy_timeout").fetchone()[0] == WAL_HYGIENE_WAIT_MS
            reader.execute("BEGIN")
            committed_count = reader.execute(WAL_HYGIENE_COUNT_SQL).fetchone()[0]
            writer.execute(WAL_HYGIENE_INSERT_SQL, (bytes(WAL_HYGIENE_PAYLOAD_BYTES),))
            assert reader.execute(WAL_HYGIENE_COUNT_SQL).fetchone()[0] == committed_count
            writer.commit()
            assert reader.execute(WAL_HYGIENE_COUNT_SQL).fetchone()[0] == committed_count
            reader.rollback()
            assert reader.execute(WAL_HYGIENE_COUNT_SQL).fetchone()[0] == committed_count + 1
            with pytest.raises(sqlite3.OperationalError):
                reader.execute(WAL_HYGIENE_MISSING_SQL)
    finally:
        writer.close()


def check_wal_checkpoint_config_keys_present(monkeypatch):
    assert 0 < config.LEDGER_WAL_CHECKPOINT_BUSY_TIMEOUT_MS * WAL_HYGIENE_SECONDS_PER_MS < config.ledger_wal_checkpoint_interval_s()
    assert config.LEDGER_WAL_AUTOCHECKPOINT_PAGES > 0
    assert 0 < config.LEDGER_READ_BUSY_TIMEOUT_MS < config.LEDGER_BUSY_TIMEOUT_MS
    source = pathlib.Path(maintenance.__file__).read_text()
    assert "PRAGMA busy_timeout=0" not in source
    assert "ledger_wal_checkpoint_busy_timeout_ms" in source
    # Each owner-facing config.json key is live, not merely declared in schema.
    settings = {
        "wal_checkpoint_busy_timeout_ms": WAL_HYGIENE_WAIT_MS,
        "wal_autocheckpoint_pages": WAL_HYGIENE_AUTOCHECKPOINT_PAGES,
        "read_busy_timeout_ms": WAL_HYGIENE_WAIT_MS,
    }
    for environment_key in (WAL_HYGIENE_CHECKPOINT_WAIT_ENV, WAL_HYGIENE_READER_WAIT_ENV,
                            WAL_HYGIENE_AUTOCHECKPOINT_ENV):
        monkeypatch.delenv(environment_key, raising=False)
    monkeypatch.setattr(config, "_cfg_get", lambda section, key, default: settings.get(key, default))
    assert config.ledger_wal_checkpoint_busy_timeout_ms() == WAL_HYGIENE_WAIT_MS
    assert config.ledger_read_busy_timeout_ms() == WAL_HYGIENE_WAIT_MS
    assert config.ledger_wal_autocheckpoint_pages() == WAL_HYGIENE_AUTOCHECKPOINT_PAGES
    monkeypatch.setenv(WAL_HYGIENE_CHECKPOINT_WAIT_ENV, str(int(config.LEDGER_WAL_CHECKPOINT_INTERVAL_DEFAULT_S
                                                            * config.LEDGER_TIMEOUT_MS_PER_S)))
    with pytest.raises(ValueError):
        config.ledger_wal_checkpoint_busy_timeout_ms()
    for environment_key, getter in ((WAL_HYGIENE_CHECKPOINT_WAIT_ENV, config.ledger_wal_checkpoint_busy_timeout_ms),
                                    (WAL_HYGIENE_READER_WAIT_ENV, config.ledger_read_busy_timeout_ms),
                                    (WAL_HYGIENE_AUTOCHECKPOINT_ENV, config.ledger_wal_autocheckpoint_pages)):
        monkeypatch.setenv(environment_key, str(0))
        with pytest.raises(ValueError):
            getter()


if __name__ == "__main__":
    sys.exit(run_pytest_style({"test_" + name: value for name, value in globals().copy().items()
                              if name.startswith("check_wal_checkpoint_")}))
