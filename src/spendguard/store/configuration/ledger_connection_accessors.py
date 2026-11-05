"""Dependency-free accessors for ledger connection configuration used by cyclic consumers."""

_open_ledger_connection = None
_ledger_db_path = None
_wal_checkpoint_interval = None
_wal_checkpoint_busy_timeout = None
_ledger_read_time_budget = None
_ledger_timeout_ms_per_s = None
_ledger_query_progress_vm_instructions = None
_ledger_connection_limit_error = None


def set_ledger_connection_accessors(open_connection, db_path, checkpoint_interval,
                                    checkpoint_busy_timeout, read_time_budget,
                                    timeout_ms_per_s, query_progress_vm_instructions,
                                    connection_limit_error):
    """Injected once by config with its ledger connection functions, constants, and error."""
    global _open_ledger_connection, _ledger_db_path, _wal_checkpoint_interval
    global _wal_checkpoint_busy_timeout, _ledger_read_time_budget, _ledger_timeout_ms_per_s
    global _ledger_query_progress_vm_instructions, _ledger_connection_limit_error
    _open_ledger_connection = open_connection
    _ledger_db_path = db_path
    _wal_checkpoint_interval = checkpoint_interval
    _wal_checkpoint_busy_timeout = checkpoint_busy_timeout
    _ledger_read_time_budget = read_time_budget
    _ledger_timeout_ms_per_s = timeout_ms_per_s
    _ledger_query_progress_vm_instructions = query_progress_vm_instructions
    _ledger_connection_limit_error = connection_limit_error


def open_wal_ledger_connection(path=None, timeout=None, *, tune=True, deadline_at=None, **options):
    if _open_ledger_connection is None:
        raise RuntimeError("import spendguard before opening a WAL ledger connection so config can wire its accessors")
    if timeout is None:
        return _open_ledger_connection(path, tune=tune, deadline_at=deadline_at, **options)
    return _open_ledger_connection(path, timeout=timeout, tune=tune, deadline_at=deadline_at, **options)


def resolve_wal_ledger_db_path():
    if _ledger_db_path is None:
        raise RuntimeError("import spendguard before resolving the WAL ledger path so config can wire its accessors")
    return _ledger_db_path()


def read_wal_checkpoint_interval_seconds():
    if _wal_checkpoint_interval is None:
        raise RuntimeError("import spendguard before reading the WAL checkpoint interval so config can wire its accessors")
    return _wal_checkpoint_interval()


def read_wal_checkpoint_busy_timeout_milliseconds():
    if _wal_checkpoint_busy_timeout is None:
        raise RuntimeError("import spendguard before reading the WAL checkpoint timeout so config can wire its accessors")
    return _wal_checkpoint_busy_timeout()


def get_wal_ledger_read_time_budget_seconds():
    if _ledger_read_time_budget is None:
        raise RuntimeError("import spendguard before reading the WAL time budget so config can wire its accessors")
    return _ledger_read_time_budget


def get_wal_ledger_timeout_milliseconds_per_second():
    if _ledger_timeout_ms_per_s is None:
        raise RuntimeError("import spendguard before reading the WAL timeout scale so config can wire its accessors")
    return _ledger_timeout_ms_per_s


def get_wal_ledger_query_progress_vm_instructions():
    if _ledger_query_progress_vm_instructions is None:
        raise RuntimeError("import spendguard before reading the WAL progress cadence so config can wire its accessors")
    return _ledger_query_progress_vm_instructions


def wal_ledger_connection_limit_error_type():
    if _ledger_connection_limit_error is None:
        raise RuntimeError("import spendguard before reading the ledger connection error type so config can wire its accessors")
    return _ledger_connection_limit_error
