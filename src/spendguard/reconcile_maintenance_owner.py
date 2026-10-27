"""Exclusive, deadline-bounded SQLite ownership for offline reconciliation.

Request cursors retain their abandonment machinery. This owner never borrows a
request connection: its native cursors live only until the offline context exits.
"""
from contextlib import contextmanager, ExitStack
from functools import partialmethod
import math
import os
import sqlite3
import time
import weakref

from spendguard import config, ledger_read

RECONCILE_MAINTENANCE_BUDGET_ENV = "SPENDGUARD_RECONCILE_MAINTENANCE_TIME_BUDGET_S"
# Whole correction transactions measured 8.3 seconds under local coverage.
# Allow slower interpreters and parallel CI scheduling; this lifetime budget is
# separate from both single-statement lock waits and the runner's file budget.
RECONCILE_MAINTENANCE_DEFAULT_TIME_BUDGET_S = 60.0
RECONCILE_MAINTENANCE_PROGRESS_DISABLED = 0
RECONCILE_MAINTENANCE_MILLISECONDS_PER_SECOND = 1000
_RECONCILE_MAINTENANCE_EXECUTION_METHODS = frozenset((
    sqlite3.Cursor.execute, sqlite3.Cursor.executemany, sqlite3.Cursor.executescript))
_RECONCILE_MAINTENANCE_LOCK_REFUSALS = frozenset((sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED))


class ReconcileMaintenanceDeadline(ledger_read.LedgerReadTimeout):
    """An offline maintenance operation exhausted its complete lifetime budget."""


class ReconcileMaintenanceRequestRefusal(ledger_read.LedgerReadError):
    """Offline ownership cannot escape request cancellation containment."""


class _ReconcileMaintenanceCursor(sqlite3.Cursor):
    """Keep native SQLite iteration while checking the owner's live boundary."""

    def _step_reconcile_maintenance_cursor(self, operation, *args, **kwargs):
        owner = self._reconcile_maintenance_owner
        owner.require_reconcile_maintenance_live()
        if operation in _RECONCILE_MAINTENANCE_EXECUTION_METHODS:
            owner.bound_reconcile_maintenance_lock_wait()
        try:
            result = operation(self, *args, **kwargs)
        except sqlite3.Error as failure:
            owner.require_reconcile_maintenance_live()
            if (owner._reconcile_wait_bound_by_deadline
                    and getattr(failure, "sqlite_errorcode", None) in _RECONCILE_MAINTENANCE_LOCK_REFUSALS):
                raise ReconcileMaintenanceDeadline(owner._reconcile_budget) from failure
            raise
        owner.require_reconcile_maintenance_live()
        return result

    execute = partialmethod(_step_reconcile_maintenance_cursor, sqlite3.Cursor.execute)
    executemany = partialmethod(_step_reconcile_maintenance_cursor, sqlite3.Cursor.executemany)
    executescript = partialmethod(_step_reconcile_maintenance_cursor, sqlite3.Cursor.executescript)
    fetchone = partialmethod(_step_reconcile_maintenance_cursor, sqlite3.Cursor.fetchone)
    fetchmany = partialmethod(_step_reconcile_maintenance_cursor, sqlite3.Cursor.fetchmany)
    fetchall = partialmethod(_step_reconcile_maintenance_cursor, sqlite3.Cursor.fetchall)
    __next__ = partialmethod(_step_reconcile_maintenance_cursor, sqlite3.Cursor.__next__)


class ReconcileMaintenanceConnection:
    """An exclusive wrapper over the reserved writer, with its own cursor lifetime."""

    def __init__(self, connection, deadline, budget):
        self._native_reconcile_connection = connection
        self._reconcile_saved_progress = getattr(connection, "_ledger_progress", (None, 0))
        parser = getattr(connection, "_ledger_statement_parser_facts", None)
        self._reconcile_saved_authorizer = (parser.authorizer_router if parser is not None
                                            else getattr(connection, "_ledger_authorizer", None))
        self._reconcile_saved_row_factory = connection.row_factory
        cursor = sqlite3.Connection.execute(connection, "PRAGMA busy_timeout")
        try:
            self._reconcile_saved_busy_timeout = cursor.fetchone()[0]
        finally:
            cursor.close()
        self._reconcile_deadline = deadline
        self._reconcile_budget = budget
        self._reconcile_progress_failure = None
        self._reconcile_cursors = weakref.WeakSet()
        self._reconcile_closed = False
        self._reconcile_complete = False
        self._reconcile_wait_bound_by_deadline = False

    def require_reconcile_maintenance_live(self):
        if self._reconcile_progress_failure is not None:
            raise self._reconcile_progress_failure
        if self._reconcile_closed:
            raise sqlite3.ProgrammingError("Offline reconciliation owner is closed")
        if self._reconcile_complete:
            raise sqlite3.ProgrammingError("Offline reconciliation corrections are already committed")
        if ledger_read.current_ledger_request_scope() is not None:
            raise ReconcileMaintenanceRequestRefusal("Offline reconciliation cannot run inside a request scope")
        connection = self._native_reconcile_connection
        config.require_ledger_home_alignment(
            connection._ledger_home_path,
            opened_path_identity=connection._ledger_opened_path_identity)
        if time.monotonic() >= self._reconcile_deadline:
            raise ReconcileMaintenanceDeadline(self._reconcile_budget)

    def poll_reconcile_maintenance_deadline(self):
        # SQLite invokes this inside one native step. Filesystem identity is
        # validated before and after that step, including commit admission;
        # repeating stat/path work at every VM poll makes offline scans costly
        # under coverage. Keep cancellation and deadline interruption fine here.
        try:
            if self._reconcile_progress_failure is not None:
                raise self._reconcile_progress_failure
            if ledger_read.current_ledger_request_scope() is not None:
                raise ReconcileMaintenanceRequestRefusal("Offline reconciliation cannot run inside a request scope")
            if time.monotonic() >= self._reconcile_deadline:
                raise ReconcileMaintenanceDeadline(self._reconcile_budget)
        except BaseException as failure:
            self._reconcile_progress_failure = failure
            return True
        return False

    def bound_reconcile_maintenance_lock_wait(self):
        """SQLite busy waits do not invoke progress callbacks; bound them too."""
        remaining = max(0, self._reconcile_deadline - time.monotonic())
        remaining_ms = int(remaining * RECONCILE_MAINTENANCE_MILLISECONDS_PER_SECOND)
        self._reconcile_wait_bound_by_deadline = remaining_ms <= config.LEDGER_BUSY_TIMEOUT_MS
        wait_ms = min(config.LEDGER_BUSY_TIMEOUT_MS, remaining_ms)
        cursor = sqlite3.Connection.execute(
            self._native_reconcile_connection, f"PRAGMA busy_timeout={wait_ms}")
        cursor.close()

    def cursor(self):
        # Allocation performs no SQL or result consumption. Every cursor step
        # checks before and after SQLite work; avoid a redundant traced check.
        cursor = sqlite3.Connection.cursor(self._native_reconcile_connection, _ReconcileMaintenanceCursor)
        cursor._reconcile_maintenance_owner = self
        self._reconcile_cursors.add(cursor)
        return cursor

    def execute(self, *args, **kwargs):
        return self.cursor().execute(*args, **kwargs)

    def executemany(self, *args, **kwargs):
        return self.cursor().executemany(*args, **kwargs)

    def executescript(self, *args, **kwargs):
        return self.cursor().executescript(*args, **kwargs)

    def _get_reconcile_maintenance_row_factory(self):
        return self._native_reconcile_connection.row_factory

    def _set_reconcile_maintenance_row_factory(self, value):
        self.require_reconcile_maintenance_live()
        self._native_reconcile_connection.row_factory = value

    row_factory = property(_get_reconcile_maintenance_row_factory,
                           _set_reconcile_maintenance_row_factory)

    @property
    def in_transaction(self):
        return self._native_reconcile_connection.in_transaction

    @property
    def total_changes(self):
        return self._native_reconcile_connection.total_changes

    def create_aggregate(self, *args, **kwargs):
        self.require_reconcile_maintenance_live()
        return self._native_reconcile_connection.create_aggregate(*args, **kwargs)

    def commit(self):
        self.require_reconcile_maintenance_live()
        self.bound_reconcile_maintenance_lock_wait()
        try:
            sqlite3.Connection.commit(self._native_reconcile_connection)
        except sqlite3.Error as failure:
            self.require_reconcile_maintenance_live()
            if (self._reconcile_wait_bound_by_deadline
                    and getattr(failure, "sqlite_errorcode", None) in _RECONCILE_MAINTENANCE_LOCK_REFUSALS):
                raise ReconcileMaintenanceDeadline(self._reconcile_budget) from failure
            raise

    def rollback(self):
        # Cleanup must remain possible after expiry, cancellation or home redirect.
        sqlite3.Connection.set_progress_handler(
            self._native_reconcile_connection, None, RECONCILE_MAINTENANCE_PROGRESS_DISABLED)
        sqlite3.Connection.rollback(self._native_reconcile_connection)
        sqlite3.Connection.set_progress_handler(
            self._native_reconcile_connection, self.poll_reconcile_maintenance_deadline,
            config.LEDGER_QUERY_PROGRESS_VM_INSTRUCTIONS)

    def commit_reconcile_maintenance_corrections(self):
        """Finish exactly once; never report refusal after a successful commit.

        Deadline checks govern commit admission, SQLite VM progress and lock
        waits. Filesystem completion of an admitted commit cannot be undone by
        a later clock check. No further SQL is allowed after this boundary.
        """
        self.commit()
        self._reconcile_complete = True

    def close_reconcile_maintenance_owner(self):
        if self._reconcile_closed:
            return
        try:
            try:
                for cursor in tuple(self._reconcile_cursors):
                    sqlite3.Cursor.close(cursor)
            finally:
                self.rollback()
        finally:
            self._reconcile_closed = True
            connection = self._native_reconcile_connection
            connection.row_factory = self._reconcile_saved_row_factory
            sqlite3.Connection.set_progress_handler(connection, *self._reconcile_saved_progress)
            sqlite3.Connection.set_authorizer(connection, self._reconcile_saved_authorizer)
            cursor = sqlite3.Connection.execute(connection,
                f"PRAGMA busy_timeout={self._reconcile_saved_busy_timeout}")
            cursor.close()

    close = close_reconcile_maintenance_owner


@contextmanager
def owned_reconcile_maintenance_connection(path=None, time_budget=None):
    """Own native cursors until rollback and writer restoration, including BaseException.

    The configurable default bounds the complete offline correction transaction.
    The caller initializes schema, begins IMMEDIATE, and commits
    only its complete correction transaction; this context never auto-commits.
    """
    if ledger_read.current_ledger_request_scope() is not None:
        raise ReconcileMaintenanceRequestRefusal("Offline reconciliation cannot borrow a request scope")
    if isinstance(time_budget, bool):
        raise ValueError("Offline reconciliation budget must be positive and finite")
    budget = (float(os.environ.get(RECONCILE_MAINTENANCE_BUDGET_ENV,
                                   RECONCILE_MAINTENANCE_DEFAULT_TIME_BUDGET_S))
              if time_budget is None else float(time_budget))
    if not math.isfinite(budget) or budget <= 0:
        raise ValueError("Offline reconciliation budget must be positive and finite")
    deadline = time.monotonic() + budget
    from .ledger_writer import exclusive_ledger_writer_connection
    with ExitStack() as reservation_stack:
        try:
            with ledger_read.ledger_request_scope(deadline_s=deadline - time.monotonic()):
                connection = reservation_stack.enter_context(exclusive_ledger_writer_connection(path))
        except ledger_read.LedgerReadError as error:
            raise ReconcileMaintenanceDeadline(budget) from error
        owner = ReconcileMaintenanceConnection(connection, deadline, budget)
        try:
            # The admission reservation excludes every worker callback. Native
            # cursors enforce this wrapper's deadline instead of request routing.
            sqlite3.Connection.set_authorizer(connection, None)
            sqlite3.Connection.set_progress_handler(
                connection, owner.poll_reconcile_maintenance_deadline,
                config.LEDGER_QUERY_PROGRESS_VM_INSTRUCTIONS)
            owner.require_reconcile_maintenance_live()
            yield owner
            if not owner._reconcile_complete:
                owner.require_reconcile_maintenance_live()
        finally:
            owner.close()
