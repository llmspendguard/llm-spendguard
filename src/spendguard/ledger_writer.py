"""Bounded single-owner ledger write queue with cross-process transaction admission.

Callbacks preserve read/modify/write transactions and return values; SQL-only queues
cannot safely express the ledger's hash-chained audit operations.

Global lock order: acquire and release subsystem locks INSIDE writer callbacks,
never around synchronous writer dispatch (including read-lease schema initialization).
Independent SQLite read leases need no subsystem lock; SQLite supplies their snapshots.
"""
import atexit
import contextvars
import contextlib
import functools
import os
import math
import queue
import threading
import pathlib
import fcntl
import time
import itertools
import sqlite3
from concurrent.futures import Future, TimeoutError as FutureTimeoutError

from . import config

LEDGER_WRITER_QUEUE_CAPACITY = 256
LEDGER_WRITER_IDLE_SECONDS = 60.0
LEDGER_WRITER_THREAD_NAME = "spendguard-ledger-writer"
LEDGER_WRITER_LOCK_SUFFIX = ".writer.lock"
LEDGER_WRITER_DEADLINE_POLL_DIVISOR = 64
_ledger_writer_registry = {}
_ledger_writer_registry_lock = threading.RLock()
_ledger_writer_local = threading.local()
_ledger_writer_registry_pid = os.getpid()
_ledger_bulk_scope_path = contextvars.ContextVar("ledger_bulk_scope_path", default=None)
_ledger_nested_savepoint_sequence = itertools.count()
LEDGER_NESTED_SAVEPOINT_PREFIX = "ledger_nested_write_"


def execute_ledger_savepoint_cleanup(connection, statement):
    """Transaction cleanup must remain possible after request abandonment."""
    native_connection = getattr(connection, "_native_reconcile_connection", connection)
    sqlite3.Connection.execute(native_connection, statement).close()


@contextlib.contextmanager
def isolate_nested_ledger_write(connection, *, sql_connection=None):
    """Make a caught inline failure atomic without owning the outer transaction.

    BEGIN keeps RELEASE from committing when this is the outer scope's first
    write. Commit deferral also covers legacy callbacks that call commit().
    A deliberate owner abort already removed every savepoint; never roll twice.
    """
    require_ledger_writer_request_live()
    statement_connection = connection if sql_connection is None else sql_connection
    if not connection.in_transaction:
        statement_connection.execute("BEGIN")
    savepoint_name = LEDGER_NESTED_SAVEPOINT_PREFIX + str(next(_ledger_nested_savepoint_sequence))
    previous_defer = ledger_connection_commit_deferred(connection)
    connection._ledger_bulk_commit_deferred = True
    try:
        statement_connection.execute(f"SAVEPOINT {savepoint_name}")
        try:
            yield
            require_ledger_writer_request_live()
            if connection.in_transaction and not getattr(connection, "_ledger_writer_transaction_aborted", False):
                execute_ledger_savepoint_cleanup(connection, f"RELEASE SAVEPOINT {savepoint_name}")
        except BaseException:
            if connection.in_transaction and not getattr(connection, "_ledger_writer_transaction_aborted", False):
                try:
                    execute_ledger_savepoint_cleanup(connection, f"ROLLBACK TO SAVEPOINT {savepoint_name}")
                    execute_ledger_savepoint_cleanup(connection, f"RELEASE SAVEPOINT {savepoint_name}")
                except BaseException:
                    # Expired cursor scopes can refuse cleanup SQL. Fail closed
                    # at the authoritative owner rather than retain any rows.
                    if current_ledger_writer_connection() is connection:
                        abort_current_ledger_writer_transaction()
                    elif connection.in_transaction:
                        connection.rollback()
                    # Keep the original attempted-write error: retry accounting
                    # must not mistake cleanup expiry for an untouched entry.
                getattr(connection, "_ledger_writer_schema_keys", set()).clear()
            raise
    finally:
        connection._ledger_bulk_commit_deferred = previous_defer


def ledger_bulk_scope_matches(path):
    """Whether this caller belongs to the reserved transaction for this file."""
    binding = _ledger_bulk_scope_path.get()
    return binding is not None and binding[0] == os.path.realpath(os.fspath(path))


def reserved_ledger_bulk_read_connection(path):
    """Return the transaction handle bound to this caller's bulk scope."""
    return _ledger_bulk_scope_path.get()[1] if ledger_bulk_scope_matches(path) else None


@contextlib.contextmanager
def bind_reserved_ledger_bulk_reads(path, connection):
    """Bind reports to the same handle as both writer-owned and maintenance bulk."""
    token = _ledger_bulk_scope_path.set((os.path.realpath(os.fspath(path)), connection))
    try:
        yield
    finally:
        _ledger_bulk_scope_path.reset(token)


def ledger_connection_commit_deferred(connection):
    """The reserved connection, rather than any facade, owns deferred commits."""
    return getattr(connection, "_ledger_bulk_commit_deferred", False)


def require_ledger_writer_request_live():
    """Reject expired callbacks before SQL and, critically, before commit."""
    from .ledger_read import current_ledger_request_scope, LedgerQueryDeadline
    scope = current_ledger_request_scope()
    if scope is not None:
        scope.require_ledger_request_live()
        if time.monotonic() >= scope.deadline_at:
            raise LedgerQueryDeadline(scope.deadline_s)


@contextlib.contextmanager
def deadline_ledger_writer_lock(lock, *, deadline_at=None):
    """Apply the request's absolute budget to every writer-side Python lock."""
    from .ledger_read import current_ledger_request_scope, LedgerQueryDeadline, effective_ledger_admission_deadline
    scope = current_ledger_request_scope()
    require_ledger_writer_request_live()
    deadline_at = effective_ledger_admission_deadline(deadline_at)
    acquired = (lock.acquire() if deadline_at is None else
                lock.acquire(timeout=max(0, deadline_at - time.monotonic())))
    if not acquired:
        raise LedgerQueryDeadline(scope.deadline_s if scope is not None else deadline_at)
    try:
        require_ledger_writer_request_live()
        yield
    finally:
        lock.release()


def reset_inherited_ledger_writer_owners():
    """Discard child-only state without unlocking a live parent's flock lease."""
    global _ledger_writer_registry_pid, _ledger_writer_registry_lock
    for inherited_owner in _ledger_writer_registry.values():
        if inherited_owner.write_lock_handle is not None:
            inherited_owner.write_lock_handle.close()
            inherited_owner.write_lock_handle = None
    _ledger_writer_registry.clear()
    _ledger_writer_local.owner = None
    _ledger_writer_registry_lock = threading.RLock()
    _ledger_writer_registry_pid = os.getpid()


os.register_at_fork(after_in_child=reset_inherited_ledger_writer_owners)


def current_ledger_writer_connection(path=None):
    """Borrow the owner handle only from its executing worker thread."""
    owner = getattr(_ledger_writer_local, "owner", None)
    if owner is None:
        return None
    if path is not None and os.path.realpath(os.fspath(path)) != owner.path:
        return None
    return owner.connection


def abort_current_ledger_writer_transaction(*, reset=False, path=None):
    """Abort the active owner once; keep its handle alive until callback unwinds."""
    connection = current_ledger_writer_connection(path)
    if connection is None:
        return False
    connection._ledger_writer_transaction_aborted = True
    if reset:
        connection._ledger_writer_reset_pending = True
    if connection.in_transaction:
        connection.rollback()
    connection._ledger_writer_schema_keys.clear()
    return True


class _LedgerWriteOwner:
    def __init__(self, path):
        self.path = path
        self.connection = None
        self.admission = threading.RLock()
        self.defer_commit = False
        self.write_lock_depth = 0
        self.write_lock_handle = None
        capacity = int(config._cfg_get("ledger", "writer_queue_capacity", LEDGER_WRITER_QUEUE_CAPACITY))
        self.idle_seconds = float(config._cfg_get("ledger", "writer_idle_seconds", LEDGER_WRITER_IDLE_SECONDS))
        if capacity <= 0 or not math.isfinite(self.idle_seconds) or self.idle_seconds <= 0:
            raise ValueError("invalid ledger writer queue capacity or idle interval")
        self.failure = None
        self.retired = False
        self.last_activity = time.monotonic()
        self.ready = threading.Event()
        self.jobs = queue.Queue(maxsize=capacity)
        startup_context = contextvars.copy_context()
        self.thread = threading.Thread(target=lambda: startup_context.run(self._serve_ledger_write_jobs),
                                       name=LEDGER_WRITER_THREAD_NAME, daemon=True)

    def _serve_ledger_write_jobs(self):
        _ledger_writer_local.owner = self
        try:
            with self.cross_process_write_lease():
                from .ledger_read import current_ledger_request_scope
                startup_scope = current_ledger_request_scope()
                self.connection = config.open_ledger_connection(
                    self.path, deadline_at=None if startup_scope is None else startup_scope.deadline_at)
            self.connection._ledger_writer_schema_keys = set()
            self.ready.set()
            while True:
                try:
                    job = self.jobs.get(timeout=self.idle_seconds)
                except queue.Empty:
                    # Never wait for admission: its holder may be waiting for
                    # this worker. Admission also protects caller-driven bulk
                    # scopes, where an empty queue does not mean an idle owner.
                    if not self.admission.acquire(blocking=False):
                        continue
                    try:
                        if self.jobs.empty() and time.monotonic() - self.last_activity >= self.idle_seconds:
                            # Close before publishing removal so a replacement
                            # cannot compete with this handle for the fd cap.
                            if self.connection is not None:
                                self.connection.close()
                            self.connection = None
                            with _ledger_writer_registry_lock:
                                self.retired = True
                                if _ledger_writer_registry.get(self.path) is self:
                                    del _ledger_writer_registry[self.path]
                            return
                    finally:
                        self.admission.release()
                    continue
                if job is None:
                    return
                callback, context, result = job
                callback_error = None
                def execute_deadline_write_job(callback=callback):
                    try:
                        with self.cross_process_write_lease():
                            require_ledger_writer_request_live()
                            if self.connection is None:
                                from .ledger_read import current_ledger_request_scope
                                reopen_scope = current_ledger_request_scope()
                                self.connection = config.open_ledger_connection(
                                    self.path, deadline_at=None if reopen_scope is None else reopen_scope.deadline_at)
                                self.connection._ledger_writer_schema_keys = set()
                            if not self.defer_commit:
                                self.connection._ledger_writer_transaction_aborted = False
                            value = callback()
                            require_ledger_writer_request_live()
                            if getattr(self.connection, "_ledger_writer_transaction_aborted", False):
                                abort_current_ledger_writer_transaction()
                            elif not self.defer_commit and self.connection.in_transaction:
                                self.connection.commit()
                            return value
                    except BaseException as exc:
                        try:
                            abort_current_ledger_writer_transaction()
                        except BaseException:
                            raise
                        raise exc
                    finally:
                        if (self.connection is not None and not self.defer_commit
                                and getattr(self.connection, "_ledger_writer_reset_pending", False)):
                            self.connection.close()
                            self.connection = None
                        self.last_activity = time.monotonic()
                        self.jobs.task_done()
                try:
                    value = context.run(execute_deadline_write_job)
                except BaseException as exc:
                    callback_error = exc
                if callback_error is not None:
                    result.set_exception(callback_error)
                else:
                    result.set_result(value)
        except BaseException as exc:
            # Startup failure must reach every admitted caller, never hang it.
            with _ledger_writer_registry_lock:
                self.failure = exc
                self.ready.set()
                if _ledger_writer_registry.get(self.path) is self:
                    del _ledger_writer_registry[self.path]
                while True:
                    try:
                        pending = self.jobs.get_nowait()
                    except queue.Empty:
                        break
                    if pending is not None:
                        pending[2].set_exception(exc)
        finally:
            if self.connection is not None:
                self.connection.close()
            _ledger_writer_local.owner = None

    @contextlib.contextmanager
    def cross_process_write_lease(self):
        """Hold OS admission until the complete transaction or maintenance scope ends.

        Owner admission serializes callers and its worker, allowing a bulk scope
        to retain the lease while nested queued jobs execute on that worker.
        """
        config.require_ledger_home_alignment(self.path)
        if self.write_lock_depth == 0:
            lock_path = pathlib.Path(self.path + LEDGER_WRITER_LOCK_SUFFIX)
            lock_path.parent.mkdir(parents=True, exist_ok=True)
            self.write_lock_handle = lock_path.open("a")
            try:
                from .ledger_read import current_ledger_request_scope
                scope = current_ledger_request_scope()
                if scope is None:
                    fcntl.flock(self.write_lock_handle.fileno(), fcntl.LOCK_EX)
                else:
                    while True:
                        require_ledger_writer_request_live()
                        try:
                            fcntl.flock(self.write_lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                            break
                        except BlockingIOError:
                            time.sleep(max(0, scope.deadline_at - time.monotonic())
                                       / LEDGER_WRITER_DEADLINE_POLL_DIVISOR)
            except BaseException:
                self.write_lock_handle.close()
                self.write_lock_handle = None
                raise
        self.write_lock_depth += 1
        try:
            yield
        finally:
            self.write_lock_depth -= 1
            if self.write_lock_depth == 0:
                handle, self.write_lock_handle = self.write_lock_handle, None
                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                finally:
                    handle.close()


@contextlib.contextmanager
def admit_live_ledger_writer(ledger_path):
    """Reserve a current owner without holding the registry across dispatch.

    Retirement and shutdown take admission before removing an owner. A caller
    that already fetched that owner retries after acquiring its admission.
    Registry lookup always releases its lock before waiting for admission.
    """
    if _ledger_writer_registry_pid != os.getpid():
        reset_inherited_ledger_writer_owners()
    while True:
        with deadline_ledger_writer_lock(_ledger_writer_registry_lock):
            owner = _ledger_writer_registry.get(ledger_path)
            if owner is None:
                owner = _LedgerWriteOwner(ledger_path)
                _ledger_writer_registry[ledger_path] = owner
                owner.thread.start()
        with deadline_ledger_writer_lock(owner.admission):
            if owner.retired:
                continue
            try:
                yield owner
            finally:
                owner.last_activity = time.monotonic()
            return


def run_ledger_write(callback, path=None):
    """Synchronously apply one callback on the sole connection for this ledger."""
    from .ledger_read import current_ledger_request_scope, LedgerQueryDeadline
    request_scope = current_ledger_request_scope()

    def remaining_writer_deadline():
        if request_scope is None:
            return None
        request_scope.require_ledger_request_live()
        remaining = request_scope.deadline_at - time.monotonic()
        if remaining <= 0:
            raise LedgerQueryDeadline(request_scope.deadline_s)
        return remaining

    remaining_writer_deadline()
    ledger_path = os.path.realpath(os.fspath(path or config.db_path()))
    if current_ledger_writer_connection(ledger_path) is not None:
        with isolate_nested_ledger_write(current_ledger_writer_connection(ledger_path)):
            return callback()
    result = Future()
    with admit_live_ledger_writer(ledger_path) as owner:
        if not owner.ready.wait(timeout=remaining_writer_deadline()):
            raise LedgerQueryDeadline(request_scope.deadline_s)
        if owner.failure is not None:
            raise owner.failure
        if not owner.thread.is_alive():
            raise RuntimeError("ledger writer stopped before admission")
        try:
            owner.jobs.put((callback, contextvars.copy_context(), result),
                           timeout=remaining_writer_deadline())
        except queue.Full as exc:
            raise LedgerQueryDeadline(request_scope.deadline_s) from exc
        while True:
            try:
                remaining = remaining_writer_deadline()
                value = result.result(timeout=owner.idle_seconds if remaining is None else remaining)
                remaining_writer_deadline()
                return value
            except FutureTimeoutError:
                remaining_writer_deadline()
                if result.done():
                    raise
                if not owner.thread.is_alive():
                    raise owner.failure or RuntimeError("ledger writer stopped with an unresolved write")


@contextlib.contextmanager
def exclusive_ledger_writer_connection(path=None):
    """Reserve the sole owner handle for bounded caller-driven maintenance.

    Admission excludes every queued callback. A barrier finishes earlier jobs
    before the caller borrows the connection; nested writer calls stay inline.
    """
    ledger_path = os.path.realpath(os.fspath(path or config.db_path()))
    if current_ledger_writer_connection(ledger_path) is not None:
        yield current_ledger_writer_connection(ledger_path)
        return
    with admit_live_ledger_writer(ledger_path) as owner:
        run_ledger_write(lambda: None, ledger_path)
        previous_owner = getattr(_ledger_writer_local, "owner", None)
        _ledger_writer_local.owner = owner
        try:
            with owner.cross_process_write_lease():
                yield owner.connection
        finally:
            try:
                if owner.connection is not None and not owner.defer_commit:
                    if getattr(owner.connection, "_ledger_writer_transaction_aborted", False):
                        abort_current_ledger_writer_transaction()
                    if getattr(owner.connection, "_ledger_writer_reset_pending", False):
                        owner.connection.close()
                        owner.connection = None
            finally:
                _ledger_writer_local.owner = previous_owner


@contextlib.contextmanager
def ledger_writer_bulk_transaction(path=None):
    """Reserve the writer through a caller-driven bulk transaction."""
    ledger_path = os.path.realpath(os.fspath(path or config.db_path()))
    bulk_scope_owns_final_commit = current_ledger_writer_connection(ledger_path) is None
    with exclusive_ledger_writer_connection(ledger_path) as connection:
        owner = _ledger_writer_local.owner
        previous_defer_commit = owner.defer_commit
        owner.defer_commit = True
        connection._ledger_bulk_commit_deferred = True
        bulk_token = _ledger_bulk_scope_path.set((ledger_path, connection))
        try:
            yield
            if bulk_scope_owns_final_commit and not previous_defer_commit:
                connection._ledger_bulk_commit_deferred = False
                require_ledger_writer_request_live()
                if getattr(connection, "_ledger_writer_transaction_aborted", False):
                    abort_current_ledger_writer_transaction()
                elif connection.in_transaction:
                    connection.commit()
        except BaseException:
            abort_current_ledger_writer_transaction()
            raise
        finally:
            _ledger_bulk_scope_path.reset(bulk_token)
            owner.defer_commit = previous_defer_commit
            connection._ledger_bulk_commit_deferred = previous_defer_commit
            if bulk_scope_owns_final_commit and not previous_defer_commit:
                connection._ledger_writer_transaction_aborted = False
                if getattr(connection, "_ledger_writer_reset_pending", False):
                    connection.close()
                    owner.connection = None


def serialize_ledger_function(function):
    """Route an existing mutation entry point without changing its public API."""
    @functools.wraps(function)
    def invoke_serialized_ledger_function(*args, **kwargs):
        return run_ledger_write(lambda: function(*args, **kwargs))
    return invoke_serialized_ledger_function


def best_effort_ledger_function(function=None, *, failure_result=None):
    """Keep optional writer startup and dispatch inside a never-raises boundary.

    Required mutations must use serialize_ledger_function instead. The recorder
    body retains its normal return and warning behavior.
    """
    if function is None:
        return functools.partial(best_effort_ledger_function, failure_result=failure_result)

    @functools.wraps(function)
    def invoke_best_effort_ledger_function(*args, **kwargs):
        try:
            return run_ledger_write(lambda: function(*args, **kwargs))
        except Exception as bookkeeping_error:
            from . import gate
            if gate.is_deliberate_stop(bookkeeping_error):
                raise
            try:
                config.warn_once(
                    f"[spendguard] {function.__module__}.{function.__name__} write failed "
                    f"({type(bookkeeping_error).__name__}) — optional bookkeeping was not recorded")
            except Exception:
                pass
            return failure_result
    return invoke_best_effort_ledger_function


def close_ledger_writers():
    """Drain admitted writes and close owner handles (shutdown or fd recycling)."""
    with _ledger_writer_registry_lock:
        owners = list(_ledger_writer_registry.values())
    for owner in owners:
        with owner.admission:
            if owner.retired:
                continue
            owner.jobs.put(None)
            if owner.thread is not threading.current_thread():
                owner.thread.join()
            with _ledger_writer_registry_lock:
                owner.retired = True
                if _ledger_writer_registry.get(owner.path) is owner:
                    del _ledger_writer_registry[owner.path]


atexit.register(close_ledger_writers)
