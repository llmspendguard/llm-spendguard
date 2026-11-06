"""Dependency-free accessors for ledger writer operations used by cyclic consumers.

This leaf lets config's connection pool invoke ledger_writer's transaction, lock,
and commit surface without importing ledger_writer and re-creating the
config<->ledger_writer cycle. ledger_writer wires its real functions during its
eager import (the set_ledger_writer_accessors call at its module end), mirroring
ledger_read_accessors and ledger_connection_accessors.
"""

_current_ledger_writer_connection = None
_run_ledger_write = None
_deadline_ledger_writer_lock = None
_abort_current_ledger_writer_transaction = None
_ledger_connection_commit_deferred = None
_require_ledger_writer_request_live = None
_isolate_nested_ledger_write = None


def set_ledger_writer_accessors(current_connection, run_write, deadline_lock,
                                abort_transaction, commit_deferred,
                                require_request_live, isolate_nested_write):
    """Injected once by ledger_writer with its connection/lock/commit surface."""
    global _current_ledger_writer_connection, _run_ledger_write, _deadline_ledger_writer_lock
    global _abort_current_ledger_writer_transaction, _ledger_connection_commit_deferred
    global _require_ledger_writer_request_live, _isolate_nested_ledger_write
    _current_ledger_writer_connection = current_connection
    _run_ledger_write = run_write
    _deadline_ledger_writer_lock = deadline_lock
    _abort_current_ledger_writer_transaction = abort_transaction
    _ledger_connection_commit_deferred = commit_deferred
    _require_ledger_writer_request_live = require_request_live
    _isolate_nested_ledger_write = isolate_nested_write


def resolve_active_ledger_writer_connection(*args, **kwargs):
    if _current_ledger_writer_connection is None:
        raise RuntimeError("import spendguard before using the ledger writer connection so ledger_writer can wire its accessors")
    return _current_ledger_writer_connection(*args, **kwargs)


def run_wired_ledger_write(*args, **kwargs):
    if _run_ledger_write is None:
        raise RuntimeError("import spendguard before running a ledger write so ledger_writer can wire its accessors")
    return _run_ledger_write(*args, **kwargs)


def acquire_ledger_writer_deadline_lock(*args, **kwargs):
    if _deadline_ledger_writer_lock is None:
        raise RuntimeError("import spendguard before acquiring the ledger writer lock so ledger_writer can wire its accessors")
    return _deadline_ledger_writer_lock(*args, **kwargs)


def abort_active_ledger_writer_transaction(*args, **kwargs):
    if _abort_current_ledger_writer_transaction is None:
        raise RuntimeError("import spendguard before aborting a ledger writer transaction so ledger_writer can wire its accessors")
    return _abort_current_ledger_writer_transaction(*args, **kwargs)


def probe_ledger_connection_commit_deferred(*args, **kwargs):
    if _ledger_connection_commit_deferred is None:
        raise RuntimeError("import spendguard before probing deferred ledger commit so ledger_writer can wire its accessors")
    return _ledger_connection_commit_deferred(*args, **kwargs)


def require_live_ledger_writer_request(*args, **kwargs):
    if _require_ledger_writer_request_live is None:
        raise RuntimeError("import spendguard before requiring a live ledger writer request so ledger_writer can wire its accessors")
    return _require_ledger_writer_request_live(*args, **kwargs)


def enter_isolated_nested_ledger_write(*args, **kwargs):
    if _isolate_nested_ledger_write is None:
        raise RuntimeError("import spendguard before isolating a nested ledger write so ledger_writer can wire its accessors")
    return _isolate_nested_ledger_write(*args, **kwargs)
