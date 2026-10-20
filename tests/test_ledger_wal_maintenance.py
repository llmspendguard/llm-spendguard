"""Real WAL growth, pinned-reader contention, interval retry and mutation guards."""
import pathlib
import sqlite3
import sys
import time
import threading

from _pytest_fixture_bridge import align_test_startup_home, run_pytest_style

align_test_startup_home(prefix=__name__)

from spendguard import config, ledger_wal_maintenance as maintenance

WAL_GUARD_ROWS = 256
WAL_GUARD_PAYLOAD_BYTES = 4096
WAL_GUARD_SQL_CREATE = "CREATE TABLE wal_guard (payload BLOB)"
WAL_GUARD_SQL_INSERT = "INSERT INTO wal_guard VALUES (?)"
WAL_GUARD_SQL_READ = "SELECT * FROM wal_guard"
WAL_GUARD_INTERVAL_S = 1.0
WAL_GUARD_CLOCK_START_S = 100.0
WAL_GUARD_REQUEST_DEADLINE_S = 0.02
WAL_GUARD_EXPIRY_WAIT_S = 2.0
WAL_GUARD_SLOW_SQL = """WITH RECURSIVE counter(n) AS
(VALUES(0) UNION ALL SELECT n+1 FROM counter) SELECT SUM(n) FROM counter"""


def _seed_wal_maintenance_fixture(ledger_path):
    writer = sqlite3.connect(ledger_path)
    writer.execute("PRAGMA journal_mode=WAL")
    writer.execute("PRAGMA wal_autocheckpoint=0")
    writer.execute(WAL_GUARD_SQL_CREATE)
    writer.executemany(WAL_GUARD_SQL_INSERT, ((bytes(WAL_GUARD_PAYLOAD_BYTES),)
                                           for _ in range(WAL_GUARD_ROWS)))
    writer.commit()
    return writer


def test_wal_checkpoint_truncates_grown_unpinned_ledger(tmp_path):
    ledger_path = tmp_path / pathlib.Path(__file__).stem
    writer = _seed_wal_maintenance_fixture(ledger_path)
    try:
        wal_path = pathlib.Path(str(ledger_path) + "-wal")
        assert wal_path.stat().st_size > WAL_GUARD_PAYLOAD_BYTES
        report = maintenance.truncate_ledger_wal_bounded(ledger_path)
        assert report.status == maintenance.WAL_CHECKPOINT_SUCCESS, report
        assert report.log_frames == report.checkpointed_frames == 0, report
        assert wal_path.stat().st_size == 0
        assert writer.execute("SELECT COUNT(*) FROM wal_guard").fetchone()[0] == WAL_GUARD_ROWS
    finally:
        writer.close()


def test_wal_checkpoint_reports_pin_then_retries_after_release(tmp_path, monkeypatch):
    ledger_path = tmp_path / pathlib.Path(__file__).stem
    writer = _seed_wal_maintenance_fixture(ledger_path)
    reader = sqlite3.connect(ledger_path)
    warnings = []
    monkeypatch.setattr(maintenance._WAL_MAINTENANCE_LOG, "warning", lambda *args: warnings.append(args))
    try:
        reader.execute("BEGIN")
        reader.execute(WAL_GUARD_SQL_READ).fetchone()
        writer.execute(WAL_GUARD_SQL_INSERT, (bytes(WAL_GUARD_PAYLOAD_BYTES),))
        writer.commit()
        clock = [WAL_GUARD_CLOCK_START_S]
        monkeypatch.setattr(maintenance.time, "monotonic", lambda: clock[0])
        monkeypatch.setattr(config, "ledger_wal_checkpoint_interval_s", lambda: WAL_GUARD_INTERVAL_S)
        first = maintenance.maintain_ledger_wal_if_due(ledger_path)
        assert first.status == maintenance.WAL_CHECKPOINT_BUSY, first
        assert warnings and warnings[-1][-1].status == maintenance.WAL_CHECKPOINT_BUSY
        assert maintenance.maintain_ledger_wal_if_due(ledger_path).status == maintenance.WAL_CHECKPOINT_SKIPPED
        reader.rollback()
        clock[0] += WAL_GUARD_INTERVAL_S
        retry = maintenance.maintain_ledger_wal_if_due(ledger_path)
        assert retry.status == maintenance.WAL_CHECKPOINT_SUCCESS, retry
        assert pathlib.Path(str(ledger_path) + "-wal").stat().st_size == 0
    finally:
        reader.close()
        writer.close()


def test_wal_checkpoint_no_truncate_mutation_is_detected(tmp_path, monkeypatch):
    import pytest

    monkeypatch.setattr(maintenance, "WAL_CHECKPOINT_SQL", "PRAGMA wal_checkpoint(PASSIVE)")
    with pytest.raises(AssertionError):
        test_wal_checkpoint_truncates_grown_unpinned_ledger(tmp_path)


def test_wal_checkpoint_surfaces_failure_and_releases_connection(tmp_path, monkeypatch):
    ledger_path = tmp_path / pathlib.Path(__file__).stem
    writer = _seed_wal_maintenance_fixture(ledger_path)
    initial_connections = config.ledger_connection_count()
    warnings = []
    monkeypatch.setattr(maintenance._WAL_MAINTENANCE_LOG, "warning", lambda *args: warnings.append(args))
    try:
        monkeypatch.setattr(maintenance, "WAL_CHECKPOINT_SQL", "SELECT * FROM nonexistent_checkpoint_fixture")
        report = maintenance.truncate_ledger_wal_bounded(ledger_path)
        assert report.status == maintenance.WAL_CHECKPOINT_FAILED
        assert report.error and "OperationalError" in report.error
        assert warnings and warnings[-1][-1].status == maintenance.WAL_CHECKPOINT_FAILED
        assert config.ledger_connection_count() == initial_connections
    finally:
        writer.close()


def test_wal_checkpoint_pool_and_commit_are_wired(tmp_path, monkeypatch):
    ledger_path = tmp_path / pathlib.Path(__file__).stem
    attempts = []
    monkeypatch.setattr(maintenance, "maintain_ledger_wal_if_due", lambda path: attempts.append(path))
    pool_key = __name__
    try:
        with config.ledger_op(pool_key, lambda connection: connection.execute(WAL_GUARD_SQL_CREATE),
                              path=ledger_path) as connection:
            connection.execute(WAL_GUARD_SQL_INSERT, (bytes(WAL_GUARD_PAYLOAD_BYTES),))
        assert attempts == [ledger_path, ledger_path]
    finally:
        config.reset_ledger_conn(pool_key)


def test_wal_checkpoint_contention_does_not_queue_callers(tmp_path):
    cadence = maintenance.LedgerWalMaintenanceCadence()
    assert cadence.lock.acquire(blocking=False)
    try:
        report = maintenance.maintain_ledger_wal_if_due(tmp_path / pathlib.Path(__file__).stem, cadence=cadence)
        assert report.status == maintenance.WAL_CHECKPOINT_SKIPPED
    finally:
        cadence.lock.release()


def test_wal_managed_request_deadline_releases_explicit_snapshot(tmp_path):
    import pytest
    from spendguard.ledger_read import ledger_request_scope, LedgerQueryDeadline

    ledger_path = tmp_path / pathlib.Path(__file__).stem
    writer = _seed_wal_maintenance_fixture(ledger_path)
    reader = config.open_ledger_connection(ledger_path)
    try:
        with pytest.raises(LedgerQueryDeadline):
            with ledger_request_scope(deadline_s=WAL_GUARD_REQUEST_DEADLINE_S):
                reader.execute("BEGIN")
                reader.execute(WAL_GUARD_SQL_READ).fetchone()
                writer.execute(WAL_GUARD_SQL_INSERT, (bytes(WAL_GUARD_PAYLOAD_BYTES),))
                writer.commit()
                reader.execute(WAL_GUARD_SLOW_SQL).fetchone()
        report = maintenance.truncate_ledger_wal_bounded(ledger_path)
        assert report.status == maintenance.WAL_CHECKPOINT_SUCCESS, report
        assert not reader.in_transaction
        assert pathlib.Path(str(ledger_path) + "-wal").stat().st_size == 0
    finally:
        reader.close()
        writer.close()


def test_wal_managed_request_write_cursor_lifetime_preserves_commit(tmp_path):
    from spendguard.ledger_read import ledger_request_scope

    ledger_path = tmp_path / pathlib.Path(__file__).stem
    writer = _seed_wal_maintenance_fixture(ledger_path)
    reader = config.open_ledger_connection(ledger_path)
    try:
        with ledger_request_scope():
            reader.execute("PRAGMA journal_mode=WAL")
            reader.execute("INSERT INTO wal_guard VALUES (?) RETURNING rowid",
                           (bytes(WAL_GUARD_PAYLOAD_BYTES),))
            reader.commit()
        assert writer.execute("SELECT COUNT(*) FROM wal_guard").fetchone()[0] == WAL_GUARD_ROWS + 1
    finally:
        reader.close()
        writer.close()


def test_wal_managed_snapshot_recovery_mutation_is_detected(tmp_path, monkeypatch):
    import pytest
    from spendguard.ledger_request_cursor import ManagedRequestCursorState

    monkeypatch.setattr(ManagedRequestCursorState, "dispose_managed_request_cursor", lambda self: None)
    with pytest.raises(AssertionError):
        test_wal_managed_request_deadline_releases_explicit_snapshot(tmp_path)


def test_wal_managed_imported_read_has_default_deadline(tmp_path, monkeypatch):
    import pytest
    from spendguard.ledger_read import LedgerQueryDeadline

    ledger_path = tmp_path / pathlib.Path(__file__).stem
    writer = _seed_wal_maintenance_fixture(ledger_path)
    reader = config.open_ledger_connection(ledger_path)
    monkeypatch.setattr(config, "LEDGER_QUERY_DEADLINE_S", WAL_GUARD_REQUEST_DEADLINE_S)
    try:
        reader.execute("BEGIN")
        reader.execute(WAL_GUARD_SQL_READ).fetchone()
        writer.execute(WAL_GUARD_SQL_INSERT, (bytes(WAL_GUARD_PAYLOAD_BYTES),))
        writer.commit()
        with pytest.raises(LedgerQueryDeadline):
            reader.execute(WAL_GUARD_SLOW_SQL).fetchone()
        report = maintenance.truncate_ledger_wal_bounded(ledger_path)
        assert report.status == maintenance.WAL_CHECKPOINT_SUCCESS, report
        assert not reader.in_transaction
    finally:
        reader.close()
        writer.close()


def test_wal_managed_idle_abandonment_releases_snapshot(tmp_path, monkeypatch):
    ledger_path = tmp_path / pathlib.Path(__file__).stem
    writer = _seed_wal_maintenance_fixture(ledger_path)
    reader = config.open_ledger_connection(ledger_path)
    monkeypatch.setattr(config, "LEDGER_QUERY_DEADLINE_S", WAL_GUARD_REQUEST_DEADLINE_S)
    try:
        reader.execute("BEGIN")
        abandoned_cursor = reader.execute(WAL_GUARD_SQL_READ)
        abandoned_cursor.fetchone()
        writer.execute(WAL_GUARD_SQL_INSERT, (bytes(WAL_GUARD_PAYLOAD_BYTES),))
        writer.commit()
        expires = time.monotonic() + WAL_GUARD_EXPIRY_WAIT_S
        while reader.in_transaction and time.monotonic() < expires:
            time.sleep(config.LEDGER_REQUEST_DISCONNECT_POLL_S)
        report = maintenance.truncate_ledger_wal_bounded(ledger_path)
        assert report.status == maintenance.WAL_CHECKPOINT_SUCCESS, report
        assert not reader.in_transaction
    finally:
        reader.close()
        writer.close()


def test_wal_request_lifetime_does_not_age_next_read_deadline(tmp_path):
    from spendguard.ledger_read import ledger_request_scope

    ledger_path = tmp_path / pathlib.Path(__file__).stem
    writer = _seed_wal_maintenance_fixture(ledger_path)
    reader = config.open_ledger_connection(ledger_path)
    try:
        with ledger_request_scope(deadline_s=WAL_GUARD_REQUEST_DEADLINE_S):
            time.sleep(WAL_GUARD_REQUEST_DEADLINE_S + config.LEDGER_REQUEST_DISCONNECT_POLL_S)
            assert reader.execute("SELECT COUNT(*) FROM wal_guard").fetchone()[0] == WAL_GUARD_ROWS
    finally:
        reader.close()
        writer.close()


def _hold_wal_guard_connection_lock(step_lock, ready, release):
    with step_lock:
        ready.set()
        assert release.wait(WAL_GUARD_EXPIRY_WAIT_S)


def test_wal_idle_expiry_retries_busy_connection_owner(tmp_path, monkeypatch):
    from spendguard import ledger_request_cursor as cursor_module

    ledger_path = tmp_path / pathlib.Path(__file__).stem
    writer = _seed_wal_maintenance_fixture(ledger_path)
    reader = config.open_ledger_connection(ledger_path)
    release = threading.Event()
    ready = threading.Event()
    warnings = []
    monkeypatch.setattr(cursor_module._MANAGED_READ_EXPIRY_LOG, "warning",
                        lambda *args: warnings.append(args))
    holder = None
    try:
        reader.execute("BEGIN")
        abandoned_cursor = reader.execute(WAL_GUARD_SQL_READ)
        abandoned_cursor.fetchone()
        state = abandoned_cursor._ledger_request_cursor_state
        writer.execute(WAL_GUARD_SQL_INSERT, (bytes(WAL_GUARD_PAYLOAD_BYTES),))
        writer.commit()
        holder = threading.Thread(target=_hold_wal_guard_connection_lock,
                                  args=(state.step_lock, ready, release))
        holder.start()
        assert ready.wait(WAL_GUARD_EXPIRY_WAIT_S)
        state.operation_deadline_at = time.monotonic()
        expires = time.monotonic() + WAL_GUARD_EXPIRY_WAIT_S
        while state.expiry_cleanup_outcome is None and time.monotonic() < expires:
            time.sleep(config.LEDGER_REQUEST_DISCONNECT_POLL_S)
        assert state.expiry_cleanup_outcome == cursor_module.MANAGED_READ_CLEANUP_DEFERRED
        assert warnings, "deferred expiry must be observable"
        assert reader.in_transaction, "busy connection owner must retain its transaction"
        release.set()
        holder.join(WAL_GUARD_EXPIRY_WAIT_S)
        expires = time.monotonic() + WAL_GUARD_EXPIRY_WAIT_S
        while reader.in_transaction and time.monotonic() < expires:
            time.sleep(config.LEDGER_REQUEST_DISCONNECT_POLL_S)
        report = maintenance.truncate_ledger_wal_bounded(ledger_path)
        assert report.status == maintenance.WAL_CHECKPOINT_SUCCESS, report
        assert state.expiry_cleanup_outcome == cursor_module.MANAGED_READ_CLEANUP_COMPLETE
    finally:
        release.set()
        if holder is not None:
            holder.join(WAL_GUARD_EXPIRY_WAIT_S)
        reader.close()
        writer.close()


def test_wal_idle_expiry_lost_retry_mutation_is_detected(tmp_path, monkeypatch):
    import pytest
    from spendguard import ledger_request_cursor as cursor_module

    original = cursor_module.ManagedRequestCursorState.release_idle_managed_request_cursor

    def lose_wal_expiry_deferred_registration(state):
        outcome = original(state)
        if outcome == cursor_module.MANAGED_READ_CLEANUP_DEFERRED:
            with state.expiry_coordinator.condition:
                state.expiry_coordinator.states.discard(state)
            return cursor_module.MANAGED_READ_CLEANUP_COMPLETE
        return outcome

    monkeypatch.setattr(cursor_module.ManagedRequestCursorState, "release_idle_managed_request_cursor",
                        lose_wal_expiry_deferred_registration)
    with pytest.raises(AssertionError):
        test_wal_idle_expiry_retries_busy_connection_owner(tmp_path, monkeypatch)


if __name__ == "__main__":
    sys.exit(run_pytest_style(globals()))
