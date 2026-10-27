"""Live-process regression for distinct-path writer descriptor/thread retirement."""
import sys
import contextlib
import faulthandler
import threading
import time

from _pytest_fixture_bridge import align_test_startup_home, run_pytest_style

align_test_startup_home(prefix=__name__)

from spendguard import config, ledger, ledger_read, ledger_writer

IDLE_REPRO_PATH_COUNT = 5
IDLE_REPRO_SECONDS = 0.1
IDLE_REPRO_INTERVALS = 10
IDLE_REPRO_POLL_DIVISOR = 10
IDLE_REPRO_EXTENSION = ".sqlite"
IDLE_REPRO_SQL = "CREATE TABLE IF NOT EXISTS retirement_probe(value INTEGER)"
IDLE_REPRO_INSERT = "INSERT INTO retirement_probe VALUES (?)"
IDLE_REPRO_SELECT = "SELECT value FROM retirement_probe"


def test_distinct_path_writers_retire_and_recreate(monkeypatch, tmp_path):
    ledger_writer.close_ledger_writers()
    baseline_connections = config.ledger_connection_count()
    baseline_threads = {thread for thread in threading.enumerate()
                        if thread.name == ledger_writer.LEDGER_WRITER_THREAD_NAME}
    original_cfg_get = config._cfg_get

    def idle_repro_configuration(section, key, default=None):
        if (section, key) == ("ledger", "writer_idle_seconds"):
            return IDLE_REPRO_SECONDS
        return original_cfg_get(section, key, default)

    monkeypatch.setattr(config, "_cfg_get", idle_repro_configuration)
    paths = [tmp_path / f"{index}{IDLE_REPRO_EXTENSION}" for index in range(IDLE_REPRO_PATH_COUNT)]
    try:
        for path in paths:
            ledger.SpendLedger(path)
        owners = list(ledger_writer._ledger_writer_registry.values())
        assert len(owners) == IDLE_REPRO_PATH_COUNT
        deadline = time.monotonic() + IDLE_REPRO_SECONDS * IDLE_REPRO_INTERVALS
        while time.monotonic() < deadline:
            live_threads = {thread for thread in threading.enumerate()
                            if thread.name == ledger_writer.LEDGER_WRITER_THREAD_NAME} - baseline_threads
            live_connections = config.ledger_connection_count() - baseline_connections
            if not live_threads and not live_connections:
                break
            time.sleep(IDLE_REPRO_SECONDS / IDLE_REPRO_POLL_DIVISOR)
        print(f"idle retirement: threads={len(live_threads)} connections={live_connections} "
              f"opened={IDLE_REPRO_PATH_COUNT}")
        assert not live_threads and live_connections == 0, "idle writer owners persisted"
        assert not ledger_writer._ledger_writer_registry
        assert all(not owner.thread.is_alive() for owner in owners)
        recreated = ledger.SpendLedger(paths[0])
        def write_recreated_idle_path():
            connection = ledger_writer.current_ledger_writer_connection()
            connection.execute(IDLE_REPRO_SQL)
            connection.execute(IDLE_REPRO_INSERT, (IDLE_REPRO_PATH_COUNT,))
            return connection.execute(IDLE_REPRO_SELECT).fetchone()[0]

        assert ledger_writer.run_ledger_write(write_recreated_idle_path, recreated.db_path) == IDLE_REPRO_PATH_COUNT
        assert ledger_writer._ledger_writer_registry[str(paths[0].resolve())] not in owners
        print("retired path recreated; writer SQL succeeded")
    finally:
        ledger_writer.close_ledger_writers()


def test_retirement_retries_concurrent_stale_admission(monkeypatch, tmp_path):
    ledger_writer.close_ledger_writers()
    path = tmp_path / f"{__name__}{IDLE_REPRO_EXTENSION}"
    ledger.SpendLedger(path)
    owner = ledger_writer._ledger_writer_registry[str(path.resolve())]
    owner.idle_seconds = IDLE_REPRO_SECONDS
    reached_admission = threading.Event()
    resume_admission = threading.Event()
    results = []
    failures = []
    original_lock = ledger_writer.deadline_ledger_writer_lock

    @contextlib.contextmanager
    def pause_stale_owner_admission(lock):
        if lock is owner.admission and threading.current_thread() is caller:
            reached_admission.set()
            assert resume_admission.wait(timeout=config.LEDGER_WRITE_TIMEOUT_S)
        with original_lock(lock):
            yield

    def write_after_idle_lookup():
        try:
            results.append(ledger_writer.run_ledger_write(lambda: IDLE_REPRO_PATH_COUNT, path))
        except BaseException as exc:
            failures.append(exc)

    caller = threading.Thread(target=write_after_idle_lookup)
    monkeypatch.setattr(ledger_writer, "deadline_ledger_writer_lock", pause_stale_owner_admission)
    # Wake the worker so it begins a wait using the shortened interval.
    ledger_writer.run_ledger_write(lambda: None, path)
    caller.start()
    try:
        assert reached_admission.wait(timeout=config.LEDGER_WRITE_TIMEOUT_S)
        owner.thread.join(timeout=config.LEDGER_WRITE_TIMEOUT_S)
        assert not owner.thread.is_alive()
        assert owner.retired
        resume_admission.set()
        caller.join(timeout=config.LEDGER_WRITE_TIMEOUT_S)
        assert not caller.is_alive()
        assert not failures, failures
        assert results == [IDLE_REPRO_PATH_COUNT]
    finally:
        resume_admission.set()
        caller.join(timeout=config.LEDGER_WRITE_TIMEOUT_S)
        ledger_writer.close_ledger_writers()


def test_idle_retirement_preserves_timed_out_inflight_work(tmp_path):
    ledger_writer.close_ledger_writers()
    path = tmp_path / f"{__name__}{IDLE_REPRO_EXTENSION}"
    ledger.SpendLedger(path)
    owner = ledger_writer._ledger_writer_registry[str(path.resolve())]
    owner.idle_seconds = IDLE_REPRO_SECONDS
    executing = threading.Event()
    release_callback = threading.Event()
    failures = []

    def hold_idle_retirement_callback():
        executing.set()
        assert release_callback.wait(timeout=config.LEDGER_WRITE_TIMEOUT_S)

    def expire_idle_retirement_caller():
        try:
            with ledger_read.ledger_request_scope(deadline_s=IDLE_REPRO_SECONDS):
                ledger_writer.run_ledger_write(hold_idle_retirement_callback, path)
        except ledger_read.LedgerQueryDeadline:
            pass
        except BaseException as exc:
            failures.append(exc)
        else:
            failures.append(AssertionError("held callback must outlast caller deadline"))

    expired_caller = threading.Thread(target=expire_idle_retirement_caller)
    expired_caller.start()
    try:
        assert executing.wait(timeout=config.LEDGER_WRITE_TIMEOUT_S)
        expired_caller.join(timeout=config.LEDGER_WRITE_TIMEOUT_S)
        assert not expired_caller.is_alive()
        assert not failures, failures
        assert owner.admission.acquire(blocking=False), "expired caller must release admission"
        owner.admission.release()
        assert not release_callback.wait(timeout=IDLE_REPRO_SECONDS * IDLE_REPRO_INTERVALS)
        assert owner.thread.is_alive() and not owner.retired
        assert ledger_writer._ledger_writer_registry[str(path.resolve())] is owner
    finally:
        release_callback.set()
        expired_caller.join(timeout=config.LEDGER_WRITE_TIMEOUT_S)
        owner.thread.join(timeout=config.LEDGER_WRITE_TIMEOUT_S)
        ledger_writer.close_ledger_writers()
    assert not owner.thread.is_alive()


def test_idle_retirement_preserves_bulk_reservation(tmp_path):
    ledger_writer.close_ledger_writers()
    path = tmp_path / f"{__name__}{IDLE_REPRO_EXTENSION}"
    ledger.SpendLedger(path)
    owner = ledger_writer._ledger_writer_registry[str(path.resolve())]
    owner.idle_seconds = IDLE_REPRO_SECONDS
    try:
        with ledger_writer.ledger_writer_bulk_transaction(path):
            connection = ledger_writer.current_ledger_writer_connection(path)
            connection.execute(IDLE_REPRO_SQL)
            connection.execute(IDLE_REPRO_INSERT, (IDLE_REPRO_PATH_COUNT,))
            threading.Event().wait(timeout=IDLE_REPRO_SECONDS * IDLE_REPRO_INTERVALS)
            assert owner.thread.is_alive() and not owner.retired
            assert ledger_writer._ledger_writer_registry[str(path.resolve())] is owner
            assert connection.execute(IDLE_REPRO_SELECT).fetchone()[0] == IDLE_REPRO_PATH_COUNT
        owner.thread.join(timeout=config.LEDGER_WRITE_TIMEOUT_S)
        assert not owner.thread.is_alive()
    finally:
        ledger_writer.close_ledger_writers()


if __name__ == "__main__":
    faulthandler.dump_traceback_later(config.LEDGER_WRITE_TIMEOUT_S * IDLE_REPRO_INTERVALS, exit=True)
    try:
        sys.exit(run_pytest_style(globals()))
    finally:
        faulthandler.cancel_dump_traceback_later()
