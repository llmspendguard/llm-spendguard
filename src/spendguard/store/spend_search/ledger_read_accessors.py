"""Accessor bridge for cyclic consumers of ledger reads.

This dependency-free leaf lets a cyclic consumer such as ``route_realized`` use
ledger_read's ``ledger_read_table_exists`` / ``read_routing_history_report`` /
``LEDGER_CALLS_TABLE`` / ``read_cost_integrity_buckets`` / ``LedgerReadSchemaError``
plus its admission-deadline resolver and ``LedgerReadError`` without importing
``ledger_read`` and re-creating the cycle. Ledger_read wires its real functions
during its eager import.
"""

_ledger_table_exists = None
_read_routing_history = None
_ledger_calls_table = None
_read_cost_integrity_buckets = None
_ledger_read_schema_error = None
_effective_ledger_admission_deadline = None
_ledger_read_error = None


def set_ledger_read_accessors(table_exists, read_routing_history, calls_table, read_cost_integrity_buckets,
                              ledger_read_schema_error, effective_admission_deadline, ledger_read_error):
    """Injected once by ledger_read with its table, cost, and admission read surface."""
    global _ledger_table_exists, _read_routing_history, _ledger_calls_table
    global _read_cost_integrity_buckets, _ledger_read_schema_error
    global _effective_ledger_admission_deadline, _ledger_read_error
    _ledger_table_exists = table_exists
    _read_routing_history = read_routing_history
    _ledger_calls_table = calls_table
    _read_cost_integrity_buckets = read_cost_integrity_buckets
    _ledger_read_schema_error = ledger_read_schema_error
    _effective_ledger_admission_deadline = effective_admission_deadline
    _ledger_read_error = ledger_read_error


def read_ledger_table_exists(table_name):
    if _ledger_table_exists is None:
        raise RuntimeError("import spendguard before probing ledger tables so ledger_read can wire its accessors")
    return _ledger_table_exists(table_name)


def read_routing_history(intent, *, since=None, as_of=None, limit=20):
    if _read_routing_history is None:
        raise RuntimeError("import spendguard before reading routing history so ledger_read can wire its accessors")
    return _read_routing_history(intent, since=since, as_of=as_of, limit=limit)


def get_ledger_calls_table():
    if _ledger_calls_table is None:
        raise RuntimeError("import spendguard before reading the calls-table name so ledger_read can wire its accessors")
    return _ledger_calls_table


def read_metered_cost_buckets(since_days, path=None):
    if _read_cost_integrity_buckets is None:
        raise RuntimeError("import spendguard before reading cost-integrity buckets so ledger_read can wire its accessors")
    return _read_cost_integrity_buckets(since_days, path)


def ledger_read_schema_error_type():
    if _ledger_read_schema_error is None:
        raise RuntimeError("import spendguard before reading the ledger schema error type so ledger_read can wire its accessors")
    return _ledger_read_schema_error


def resolve_ledger_admission_deadline(deadline_at=None):
    if _effective_ledger_admission_deadline is None:
        raise RuntimeError("import spendguard before resolving a ledger deadline so ledger_read can wire its accessors")
    return _effective_ledger_admission_deadline(deadline_at)


def ledger_read_error_type():
    if _ledger_read_error is None:
        raise RuntimeError("import spendguard before reading the ledger read error type so ledger_read can wire its accessors")
    return _ledger_read_error
