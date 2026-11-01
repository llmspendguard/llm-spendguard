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
WAL_GUARD_CURSOR_REFERENCES_PER_STEP = 1
WAL_GUARD_DEADLINE_RESTORATION_ERROR = "deadline fault leaked into checkpoint verification"
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
    checkpoint_query_deadline = config.LEDGER_QUERY_DEADLINE_S
    try:
        with monkeypatch.context() as deadline_fault:
            deadline_fault.setattr(config, "LEDGER_QUERY_DEADLINE_S", WAL_GUARD_REQUEST_DEADLINE_S)
            reader.execute("BEGIN")
            reader.execute(WAL_GUARD_SQL_READ).fetchone()
            writer.execute(WAL_GUARD_SQL_INSERT, (bytes(WAL_GUARD_PAYLOAD_BYTES),))
            writer.commit()
            with pytest.raises(LedgerQueryDeadline):
                reader.execute(WAL_GUARD_SLOW_SQL).fetchone()
        assert not reader.in_transaction
        assert config.LEDGER_QUERY_DEADLINE_S == checkpoint_query_deadline, WAL_GUARD_DEADLINE_RESTORATION_ERROR
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
    checkpoint_query_deadline = config.LEDGER_QUERY_DEADLINE_S
    try:
        with monkeypatch.context() as deadline_fault:
            deadline_fault.setattr(config, "LEDGER_QUERY_DEADLINE_S", WAL_GUARD_REQUEST_DEADLINE_S)
            reader.execute("BEGIN")
            abandoned_cursor = reader.execute(WAL_GUARD_SQL_READ)
            abandoned_cursor.fetchone()
            writer.execute(WAL_GUARD_SQL_INSERT, (bytes(WAL_GUARD_PAYLOAD_BYTES),))
            writer.commit()
            expires = time.monotonic() + WAL_GUARD_EXPIRY_WAIT_S
            while reader.in_transaction and time.monotonic() < expires:
                time.sleep(config.LEDGER_REQUEST_DISCONNECT_POLL_S)
        assert not reader.in_transaction
        assert config.LEDGER_QUERY_DEADLINE_S == checkpoint_query_deadline, WAL_GUARD_DEADLINE_RESTORATION_ERROR
        report = maintenance.truncate_ledger_wal_bounded(ledger_path)
        assert report.status == maintenance.WAL_CHECKPOINT_SUCCESS, report
        assert not reader.in_transaction
    finally:
        reader.close()
        writer.close()


def test_wal_checkpoint_fault_deadline_leak_mutation_is_detected(tmp_path, monkeypatch):
    """Retaining the read fault across verification must fail independently of CPU timing."""
    from contextlib import contextmanager
    import pytest

    for deadline_guard in (test_wal_managed_imported_read_has_default_deadline,
                           test_wal_managed_idle_abandonment_releases_snapshot):
        guard_path = tmp_path / deadline_guard.__name__
        guard_path.mkdir()
        with monkeypatch.context() as deadline_leak:
            @contextmanager
            def retain_wal_fault_deadline_through_verification():
                yield deadline_leak

            deadline_leak.setattr(deadline_leak, "context", retain_wal_fault_deadline_through_verification)
            with pytest.raises(AssertionError, match=WAL_GUARD_DEADLINE_RESTORATION_ERROR):
                deadline_guard(guard_path, deadline_leak)


def test_wal_expired_request_refuses_read_and_next_request_recovers(tmp_path):
    import pytest
    from spendguard.ledger_read import ledger_request_scope, LedgerQueryDeadline

    ledger_path = tmp_path / pathlib.Path(__file__).stem
    writer = _seed_wal_maintenance_fixture(ledger_path)
    reader = config.open_ledger_connection(ledger_path)
    try:
        with ledger_request_scope(deadline_s=WAL_GUARD_REQUEST_DEADLINE_S):
            time.sleep(WAL_GUARD_REQUEST_DEADLINE_S + config.LEDGER_REQUEST_DISCONNECT_POLL_S)
            with pytest.raises(LedgerQueryDeadline):
                reader.execute("SELECT COUNT(*) FROM wal_guard").fetchone()
        with ledger_request_scope(deadline_s=WAL_GUARD_EXPIRY_WAIT_S):
            assert reader.execute("SELECT COUNT(*) FROM wal_guard").fetchone()[0] == WAL_GUARD_ROWS
    finally:
        reader.close()
        writer.close()


def test_wal_maintenance_skips_not_raises_under_expired_request_scope(tmp_path):
    """Best-effort maintenance must SKIP, never propagate a serving request's expired read deadline.

    A long serving turn can hold a request-lifetime ledger_request_scope whose absolute deadline has passed by
    the time a pool open triggers maintenance; raising that out of a non-mandatory checkpoint would fail an
    otherwise-complete operation. Prove the precondition is real (the ambient scope genuinely raises on a
    mandatory admission read), then assert maintenance returns SKIPPED rather than raising, and recovers under a
    fresh scope.
    """
    from spendguard.ledger_read import (ledger_request_scope, effective_ledger_admission_deadline,
                                         ledger_evidence_persistence_scope, LedgerReadError)
    ledger_path = tmp_path / pathlib.Path(__file__).stem
    writer = _seed_wal_maintenance_fixture(ledger_path)
    isolated_cadence = maintenance.LedgerWalMaintenanceCadence()
    try:
        with ledger_request_scope(deadline_s=WAL_GUARD_REQUEST_DEADLINE_S):
            time.sleep(WAL_GUARD_REQUEST_DEADLINE_S + config.LEDGER_REQUEST_DISCONNECT_POLL_S)
            precondition_raised = False
            try:
                effective_ledger_admission_deadline()
            except LedgerReadError:
                precondition_raised = True
            assert precondition_raised, "precondition not met: the ambient request scope was not actually expired"
            report = maintenance.maintain_ledger_wal_if_due(ledger_path, cadence=isolated_cadence)
            assert report.status == maintenance.WAL_CHECKPOINT_SKIPPED, report
        with ledger_evidence_persistence_scope():
            recovered = maintenance.maintain_ledger_wal_if_due(ledger_path,
                                                               cadence=maintenance.LedgerWalMaintenanceCadence())
            assert recovered.status in (maintenance.WAL_CHECKPOINT_SUCCESS, maintenance.WAL_CHECKPOINT_BUSY), recovered
    finally:
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


def test_wal_cursor_fetches_retain_one_expiry_registration(tmp_path, monkeypatch):
    """A whole scan must not wake the expiry worker separately for every row."""
    from spendguard import ledger_request_cursor as cursor_module

    ledger_path = tmp_path / pathlib.Path(__file__).stem
    writer = _seed_wal_maintenance_fixture(ledger_path)
    reader = config.open_ledger_connection(ledger_path)
    registrations = []
    original_registration = cursor_module._register_managed_read_expiry

    def count_wal_scan_expiry_registrations(state, coordinator):
        registrations.append(state)
        return original_registration(state, coordinator)

    monkeypatch.setattr(cursor_module, "_register_managed_read_expiry", count_wal_scan_expiry_registrations)
    try:
        cursor = reader.execute(WAL_GUARD_SQL_READ)
        prepared_registrations = tuple(registrations)
        assert prepared_registrations
        original_deadline = cursor._ledger_request_cursor_state.operation_deadline_at
        assert sum(1 for _ in cursor) == WAL_GUARD_ROWS
        assert tuple(registrations) == prepared_registrations
        assert cursor._ledger_request_cursor_state.operation_deadline_at == original_deadline
    finally:
        reader.close()
        writer.close()


def test_wal_native_step_retains_one_cursor_referent(tmp_path, monkeypatch):
    """The native step owns one strong cursor reference throughout callbacks."""
    from spendguard import ledger_request_cursor as cursor_module

    ledger_path = tmp_path / pathlib.Path(__file__).stem
    writer = _seed_wal_maintenance_fixture(ledger_path)
    reader = config.open_ledger_connection(ledger_path)
    try:
        cursor = reader.execute(WAL_GUARD_SQL_READ)
        observed_state = cursor._ledger_request_cursor_state
        original_referent = cursor_module.ManagedRequestCursorState._request_cursor_referent.fget
        references = []

        def count_wal_native_step_cursor_references(state):
            if state is observed_state:
                references.append(state)
            return original_referent(state)

        monkeypatch.setattr(cursor_module.ManagedRequestCursorState, "_request_cursor_referent",
                            property(count_wal_native_step_cursor_references))
        cursor.fetchone()
        assert len(references) == WAL_GUARD_CURSOR_REFERENCES_PER_STEP
        references.clear()
        cursor.execute(WAL_GUARD_SQL_READ)
        assert len(references) == WAL_GUARD_CURSOR_REFERENCES_PER_STEP
    finally:
        reader.close()
        writer.close()


# A virtual work clock makes callback density reproducible under coverage and
# CPU contention, while actual SQLite executes every opcode and re-registration.
WAL_ADAPTIVE_LONG_BUDGET_S = 2.0
WAL_ADAPTIVE_WORK_UNITS = 10000
WAL_ADAPTIVE_UNIT_SECONDS = WAL_GUARD_REQUEST_DEADLINE_S / WAL_ADAPTIVE_WORK_UNITS
WAL_ADAPTIVE_FAULT_SLOWDOWN = 100
WAL_ADAPTIVE_CALLBACK_RATIO = 2
WAL_ADAPTIVE_FIXED_INTERVAL_MUTATIONS = (100, 2000)
WAL_ADAPTIVE_SQL = """WITH RECURSIVE work(n) AS
(VALUES(1) UNION ALL SELECT n+1 FROM work WHERE n<?)
SELECT SUM(adaptive_work(n)) FROM work"""


def _measure_wal_adaptive_callback_density(deadline_s):
    from spendguard.ledger_deadline_polling import LedgerDeadlinePolling

    work_clock = [0.0]
    callback_count = [0]
    polling = LedgerDeadlinePolling(deadline_s, work_clock[0])
    connection = sqlite3.connect(":memory:")

    def advance_wal_adaptive_work_clock(value):
        work_clock[0] += WAL_ADAPTIVE_UNIT_SECONDS
        return value

    def observe_wal_adaptive_sqlite_callback():
        callback_count[0] += 1
        connection.set_progress_handler(observe_wal_adaptive_sqlite_callback,
            polling.advance_ledger_deadline_poll(work_clock[0]))
        return 0

    try:
        connection.create_function("adaptive_work", 1, advance_wal_adaptive_work_clock)
        connection.set_progress_handler(observe_wal_adaptive_sqlite_callback, polling.interval)
        result = connection.execute(WAL_ADAPTIVE_SQL, (WAL_ADAPTIVE_WORK_UNITS,)).fetchone()[0]
        assert result == sum(range(1, WAL_ADAPTIVE_WORK_UNITS + 1))
        return callback_count[0]
    finally:
        connection.close()


def test_wal_adaptive_polling_measures_deadline_density():
    tight_checks = _measure_wal_adaptive_callback_density(WAL_GUARD_REQUEST_DEADLINE_S)
    long_checks = _measure_wal_adaptive_callback_density(WAL_ADAPTIVE_LONG_BUDGET_S)
    assert tight_checks > long_checks * WAL_ADAPTIVE_CALLBACK_RATIO, (tight_checks, long_checks)
    print(f"adaptive SQLite callbacks: tight={tight_checks}, long={long_checks}")


def test_wal_adaptive_polling_fixed_interval_mutation_detected(monkeypatch):
    import pytest
    from spendguard import ledger_deadline_polling as polling_module

    for fixed_interval in WAL_ADAPTIVE_FIXED_INTERVAL_MUTATIONS:
        with monkeypatch.context() as fixed_patch:
            fixed_patch.setattr(polling_module, "LEDGER_DEADLINE_INITIAL_VM_INTERVAL", fixed_interval)
            fixed_patch.setattr(polling_module.LedgerDeadlinePolling, "advance_ledger_deadline_poll",
                                lambda self, now: self.interval)
            with pytest.raises(AssertionError):
                test_wal_adaptive_polling_measures_deadline_density()


def test_wal_adaptive_polling_shrinks_after_slow_work():
    from spendguard.ledger_deadline_polling import LedgerDeadlinePolling

    polling = LedgerDeadlinePolling(WAL_GUARD_REQUEST_DEADLINE_S, 0.0)
    first_sample = polling.target_poll_seconds
    prior_interval = polling.advance_ledger_deadline_poll(first_sample)
    slower_interval = polling.advance_ledger_deadline_poll(
        first_sample + polling.target_poll_seconds * WAL_ADAPTIVE_FAULT_SLOWDOWN)
    assert slower_interval < prior_interval


def test_wal_adaptive_polling_both_owners_interrupt_slow_scan(tmp_path, monkeypatch):
    import pytest
    from spendguard import ledger_read
    from spendguard.ledger_deadline_polling import LedgerDeadlinePolling

    ledger_path = tmp_path / pathlib.Path(__file__).stem
    writer = _seed_wal_maintenance_fixture(ledger_path)
    reader = config.open_ledger_connection(ledger_path)
    observed_intervals = []
    original_advance = LedgerDeadlinePolling.advance_ledger_deadline_poll

    def record_wal_deadline_adaptation(polling, now):
        observed_intervals.append(polling.interval)
        return original_advance(polling, now)

    monkeypatch.setattr(LedgerDeadlinePolling, "advance_ledger_deadline_poll", record_wal_deadline_adaptation)
    try:
        with pytest.raises(ledger_read.LedgerReadTimeout):
            ledger_read.read_ledger_sql(ledger_read.LedgerSQLReadRequest(
                WAL_GUARD_SLOW_SQL, time_budget=WAL_GUARD_REQUEST_DEADLINE_S), ledger_path)
        assert observed_intervals, "owned bounded read never adapted"
        observed_intervals.clear()
        with ledger_read.ledger_request_scope(deadline_s=WAL_GUARD_REQUEST_DEADLINE_S):
            with pytest.raises(ledger_read.LedgerQueryDeadline):
                reader.execute(WAL_GUARD_SLOW_SQL).fetchone()
        assert observed_intervals, "managed request read never adapted"
        assert maintenance.truncate_ledger_wal_bounded(ledger_path).status == maintenance.WAL_CHECKPOINT_SUCCESS
    finally:
        reader.close()
        writer.close()


def test_wal_connection_close_joins_idle_expiry_owner(tmp_path, monkeypatch):
    """Close must join the owner even after rollback has cleared in_transaction."""
    import threading
    from contextlib import contextmanager

    ledger_path = tmp_path / pathlib.Path(config.db_path()).name
    writer = _seed_wal_maintenance_fixture(ledger_path)
    reader = config.open_ledger_connection(ledger_path)
    native_step_lock = reader._ledger_request_step_lock
    close_attempted = threading.Event()
    close_completed = threading.Event()
    close_errors = []

    @contextmanager
    def observe_wal_close_step_lock_entry():
        close_attempted.set()
        with native_step_lock:
            yield

    def close_wal_reader_during_expiry_restoration():
        try:
            reader.close()
        except BaseException as error:
            close_errors.append(error)
        finally:
            close_completed.set()

    closer = threading.Thread(target=close_wal_reader_during_expiry_restoration)
    native_step_lock.acquire()
    monkeypatch.setattr(reader, "_ledger_request_step_lock", observe_wal_close_step_lock_entry())
    try:
        closer.start()
        assert close_attempted.wait(WAL_GUARD_EXPIRY_WAIT_S), "close bypassed the expiry owner lock"
        assert not close_completed.is_set(), "close invalidated an owned connection"
        assert not reader.in_transaction
        sqlite3.Connection.set_progress_handler(reader, None, 0)
    finally:
        native_step_lock.release()
        closer.join(WAL_GUARD_EXPIRY_WAIT_S)
        monkeypatch.setattr(reader, "_ledger_request_step_lock", native_step_lock)
        reader.close()
        writer.close()
    assert not closer.is_alive()
    assert close_completed.is_set()
    assert not close_errors


def test_wal_connection_close_owner_bypass_mutation_is_detected(tmp_path, monkeypatch):
    import pytest
    from contextlib import nullcontext

    guarded_close = config._CountedLedgerConnection.close

    def bypass_wal_close_owner_synchronization(connection):
        observed_lock = connection._ledger_request_step_lock
        connection._ledger_request_step_lock = nullcontext()
        try:
            return guarded_close(connection)
        finally:
            connection._ledger_request_step_lock = observed_lock

    with monkeypatch.context() as close_owner_mutation:
        close_owner_mutation.setattr(config._CountedLedgerConnection, "close", bypass_wal_close_owner_synchronization)
        with pytest.raises(AssertionError, match="close bypassed the expiry owner lock"):
            test_wal_connection_close_joins_idle_expiry_owner(tmp_path, close_owner_mutation)


if __name__ == "__main__":
    sys.exit(run_pytest_style(globals()))
