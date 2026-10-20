"""SQLite-authorized request read deadlines for shared managed connections.

The SQLite parser supplies operation codes; no SQL text heuristics decide whether
a statement writes. Writes retain their transaction ownership and are never
registered for transport interruption.
"""
import sqlite3
import logging
import threading
import time
import weakref

from . import config

_REQUEST_CURSOR_WRITE_ACTIONS = frozenset((
    sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE,
    sqlite3.SQLITE_CREATE_INDEX, sqlite3.SQLITE_CREATE_TABLE,
    sqlite3.SQLITE_CREATE_TEMP_INDEX, sqlite3.SQLITE_CREATE_TEMP_TABLE,
    sqlite3.SQLITE_CREATE_TEMP_TRIGGER, sqlite3.SQLITE_CREATE_TEMP_VIEW,
    sqlite3.SQLITE_CREATE_TRIGGER, sqlite3.SQLITE_CREATE_VIEW,
    sqlite3.SQLITE_DROP_INDEX, sqlite3.SQLITE_DROP_TABLE,
    sqlite3.SQLITE_DROP_TEMP_INDEX, sqlite3.SQLITE_DROP_TEMP_TABLE,
    sqlite3.SQLITE_DROP_TEMP_TRIGGER, sqlite3.SQLITE_DROP_TEMP_VIEW,
    sqlite3.SQLITE_DROP_TRIGGER, sqlite3.SQLITE_DROP_VIEW,
    sqlite3.SQLITE_ALTER_TABLE, sqlite3.SQLITE_REINDEX,
    sqlite3.SQLITE_ANALYZE, sqlite3.SQLITE_CREATE_VTABLE,
    sqlite3.SQLITE_DROP_VTABLE, sqlite3.SQLITE_ATTACH, sqlite3.SQLITE_DETACH,
))
_MANAGED_READ_EXPIRY_THREAD_NAME = "spendguard-ledger-read-expiry"
_MANAGED_READ_EXPIRY_LOG = logging.getLogger(__name__)
MANAGED_READ_CLEANUP_COMPLETE = "released"
MANAGED_READ_CLEANUP_DEFERRED = "deferred_connection_busy"


class ManagedReadExpiryCoordinator:
    """Own the process expiry worker and its weak cursor registrations."""

    def __init__(self):
        self.condition = threading.Condition()
        self.states = weakref.WeakSet()
        self.worker = None


_DEFAULT_MANAGED_READ_EXPIRY_COORDINATOR = ManagedReadExpiryCoordinator()


def _sweep_managed_read_deadlines(coordinator):
    """One process worker releases idle snapshots without queuing behind writers."""
    while True:
        with coordinator.condition:
            states = tuple(coordinator.states)
            if not states:
                coordinator.condition.wait()
                continue
        now = time.monotonic()
        for state in states:
            if state.scope is not None and now >= state.operation_deadline_at:
                state.read_deadline_expired = True
                outcome = state.release_idle_managed_request_cursor()
                if outcome == MANAGED_READ_CLEANUP_DEFERRED:
                    # Keep the registration: another sweep must retry after the
                    # unrelated connection owner releases the stepping lock.
                    with coordinator.condition:
                        coordinator.states.add(state)
        with coordinator.condition:
            coordinator.condition.wait(config.LEDGER_REQUEST_DISCONNECT_POLL_S)


def _register_managed_read_expiry(state, coordinator):
    with coordinator.condition:
        coordinator.states.add(state)
        if coordinator.worker is None:
            coordinator.worker = threading.Thread(
                target=_sweep_managed_read_deadlines, args=(coordinator,),
                name=_MANAGED_READ_EXPIRY_THREAD_NAME, daemon=True)
            coordinator.worker.start()
        coordinator.condition.notify()


class ManagedRequestCursorState:
    """Own one cursor's stepping lock, parser facts and request registration."""

    def __init__(self, cursor, expiry_coordinator=_DEFAULT_MANAGED_READ_EXPIRY_COORDINATOR):
        self.expiry_coordinator = expiry_coordinator
        self._managed_cursor_ref = weakref.ref(cursor)
        connection = cursor.connection
        if not hasattr(connection, "_ledger_request_step_lock"):
            connection._ledger_request_step_lock = threading.RLock()
        self.step_lock = connection._ledger_request_step_lock
        self.scope = None
        self.writes = False
        self.abort_error = None
        self.operation_deadline_at = None
        self.read_deadline_expired = False
        self.deadline_duration_s = None
        self.expiry_cleanup_outcome = None

    @property
    def _request_cursor_referent(self):
        return self._managed_cursor_ref()

    def release_idle_managed_request_cursor(self):
        if not self.step_lock.acquire(blocking=False):
            if self.expiry_cleanup_outcome != MANAGED_READ_CLEANUP_DEFERRED:
                _MANAGED_READ_EXPIRY_LOG.warning("Ledger read expiry deferred: connection busy; retry scheduled")
            self.expiry_cleanup_outcome = MANAGED_READ_CLEANUP_DEFERRED
            return self.expiry_cleanup_outcome
        try:
            self.dispose_managed_request_cursor()
            self.expiry_cleanup_outcome = MANAGED_READ_CLEANUP_COMPLETE
            return self.expiry_cleanup_outcome
        finally:
            self.step_lock.release()

    def dispose_managed_request_cursor(self):
        scope, self.scope = self.scope, None
        if scope is None:
            return
        with self.expiry_coordinator.condition:
            self.expiry_coordinator.states.discard(self)
        if self._request_cursor_referent is None:
            return
        connection = self._request_cursor_referent.connection
        scope.release_request_read_cursor(self._request_cursor_referent)
        try:
            sqlite3.Cursor.close(self._request_cursor_referent)
            transaction_open = connection.in_transaction
        except sqlite3.ProgrammingError:
            return  # an owned bounded reader already closed this connection
        # Only a parser-proven read-only explicit transaction can be rolled back.
        if transaction_open and not getattr(connection, "_ledger_transaction_has_writes", True):
            previous_progress, previous_interval = getattr(connection, "_ledger_progress", (None, 0))
            sqlite3.Connection.set_progress_handler(connection, None, 0)
            try:
                sqlite3.Connection.rollback(connection)
            finally:
                sqlite3.Connection.set_progress_handler(connection, previous_progress, previous_interval)

    def dispatch_managed_request_cursor_step(self, operation, *args, **kwargs):
        from .ledger_read import (current_ledger_request_scope, LedgerQueryDeadline,
                                  LedgerReadError, LedgerRequestReadScope)

        connection = self._request_cursor_referent.connection
        if getattr(connection, "_ledger_owned_read_deadline", False):
            return operation(self._request_cursor_referent, *args, **kwargs)
        with self.step_lock:
            scope = self.scope or current_ledger_request_scope()
            preparing = operation in (sqlite3.Cursor.execute, sqlite3.Cursor.executemany,
                                      sqlite3.Cursor.executescript)
            if scope is None:
                if operation is sqlite3.Cursor.executescript:
                    return operation(self._request_cursor_referent, *args, **kwargs)
                scope = LedgerRequestReadScope(config.LEDGER_QUERY_DEADLINE_S)
            if operation is sqlite3.Cursor.executescript:
                raise LedgerReadError("Request scripts require separately owned bounded statements")
            if self.operation_deadline_at is None:
                self.operation_deadline_at = time.monotonic() + scope.deadline_s
            if preparing:
                self.operation_deadline_at = time.monotonic() + scope.deadline_s
                self.deadline_duration_s = scope.deadline_s
                self.read_deadline_expired = False
                if self.scope is not None:
                    self.scope.release_request_read_cursor(self._request_cursor_referent)
                    self.scope = None
                self.writes = False
                if not connection.in_transaction:
                    connection._ledger_transaction_has_writes = False
                elif not hasattr(connection, "_ledger_transaction_has_writes"):
                    connection._ledger_transaction_has_writes = True
            self.abort_error = None
            if self.read_deadline_expired:
                raise LedgerQueryDeadline(self.deadline_duration_s)
            previous_authorizer = getattr(connection, "_ledger_authorizer", None)
            previous_progress, previous_interval = getattr(connection, "_ledger_progress", (None, 0))
            interval = config.LEDGER_QUERY_PROGRESS_VM_INSTRUCTIONS
            if previous_progress is not None and previous_interval > 0:
                interval = min(interval, previous_interval)
            progress_instructions = 0
            registered = False

            def authorize_request_cursor_statement(action, first, second, database, source):
                if action in _REQUEST_CURSOR_WRITE_ACTIONS:
                    self.writes = True
                    connection._ledger_transaction_has_writes = True
                if previous_authorizer is not None:
                    return previous_authorizer(action, first, second, database, source)
                return sqlite3.SQLITE_OK

            def enforce_request_cursor_progress():
                nonlocal registered, progress_instructions
                if not self.writes:
                    try:
                        scope.require_ledger_request_live()
                        if time.monotonic() >= self.operation_deadline_at:
                            raise LedgerQueryDeadline(scope.deadline_s)
                        if not registered:
                            scope.register_request_read_connection(connection)
                            registered = True
                    except LedgerReadError as error:
                        self.abort_error = error
                        return True
                if previous_progress is not None:
                    progress_instructions += interval
                    if progress_instructions >= previous_interval:
                        progress_instructions = 0
                        return previous_progress()
                return False

            try:
                # Resetting the authorizer invalidates SQLite's statement cache:
                # every execution supplies fresh parser facts, including repeats.
                if preparing:
                    sqlite3.Connection.set_authorizer(connection, authorize_request_cursor_statement)
                sqlite3.Connection.set_progress_handler(connection, enforce_request_cursor_progress, interval)
                result = operation(self._request_cursor_referent, *args, **kwargs)
                if not self.writes:
                    scope.require_ledger_request_live()
                    if time.monotonic() >= self.operation_deadline_at:
                        raise LedgerQueryDeadline(scope.deadline_s)
                    if self._request_cursor_referent.description is not None:
                        self.scope = scope
                        scope.register_request_read_cursor(self._request_cursor_referent)
                        _register_managed_read_expiry(self, self.expiry_coordinator)
                return result
            except BaseException as error:
                if not self.writes:
                    self.scope = scope
                    self.dispose_managed_request_cursor()
                    scope.require_ledger_request_live()
                    if time.monotonic() >= self.operation_deadline_at:
                        raise LedgerQueryDeadline(scope.deadline_s) from error
                if self.abort_error is not None:
                    raise self.abort_error from error
                raise
            finally:
                if registered:
                    scope.release_request_read_connection(connection)
                sqlite3.Connection.set_progress_handler(connection, previous_progress, previous_interval)
                if preparing:
                    sqlite3.Connection.set_authorizer(connection, previous_authorizer)
