"""Accessor bridge for cyclic consumers of ledger reads.

This dependency-free leaf lets a cyclic consumer such as ``route_realized`` use
ledger_read's ``ledger_read_table_exists`` / ``read_routing_history_report`` /
``LEDGER_CALLS_TABLE`` without importing ``ledger_read`` and re-creating the
cycle. Ledger_read wires its real functions during its eager import.
"""

_ledger_table_exists = None
_read_routing_history = None
_ledger_calls_table = None


def set_ledger_read_accessors(table_exists, read_routing_history, calls_table):
    """Injected once by ledger_read at its import with its table probe, routing-history read, and calls-table name."""
    global _ledger_table_exists, _read_routing_history, _ledger_calls_table
    _ledger_table_exists = table_exists
    _read_routing_history = read_routing_history
    _ledger_calls_table = calls_table


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
