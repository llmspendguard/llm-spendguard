"""Dependency-free accessor bridge for cyclic consumers of ledger reads.

This dependency-free leaf lets a cyclic consumer such as ``route_realized`` use
ledger_read's ``ledger_read_table_exists`` / ``read_routing_history_report`` /
``LEDGER_CALLS_TABLE`` / ``read_cost_integrity_buckets`` / ``LedgerReadSchemaError``
plus its admission-deadline resolver and ``LedgerReadError`` without importing
``ledger_read`` and re-creating the cycle. Ledger_read wires its real functions
during its eager import.
"""

import threading

import functools
import sys
import types


def _late_bind_ledger_read_module_function(resolver):
    """Preserve module-attribute dispatch while retaining safe callable fallback."""
    resolver_module_name = getattr(resolver, "__module__", None)
    resolver_function_name = getattr(resolver, "__name__", None)
    resolver_qualified_name = getattr(resolver, "__qualname__", None)
    is_top_level_function = (
        isinstance(resolver, types.FunctionType)
        and isinstance(resolver_module_name, str)
        and isinstance(resolver_function_name, str)
        and resolver_function_name != "<lambda>"
        and resolver_qualified_name == resolver_function_name
    )
    if not is_top_level_function:
        return resolver

    @functools.wraps(resolver)
    def call_current_ledger_read_module_function(*args, **kwargs):
        resolver_module = sys.modules.get(resolver_module_name)
        current_resolver = getattr(resolver_module, resolver_function_name, None)
        if not callable(current_resolver):
            current_resolver = resolver
        return current_resolver(*args, **kwargs)

    return call_current_ledger_read_module_function

_ledger_read_accessor_lock = threading.Lock()
_ledger_read_resolvers = {}


def __getattr__(attribute_name):
    """Materialize resolver APIs from their fixed ``set_``/``resolve_`` protocol."""
    if attribute_name.startswith("set_") and attribute_name.endswith("_resolver"):
        symbol_name = attribute_name[len("set_"):-len("_resolver")]

        def set_ledger_read_symbol_resolver(resolver):
            with _ledger_read_accessor_lock:
                _ledger_read_resolvers[symbol_name] = _late_bind_ledger_read_module_function(resolver)

        set_ledger_read_symbol_resolver.__name__ = attribute_name
        globals()[attribute_name] = set_ledger_read_symbol_resolver
        return set_ledger_read_symbol_resolver
    if attribute_name.startswith("resolve_"):
        symbol_name = attribute_name[len("resolve_"):]

        def resolve_ledger_read_symbol(*args, **kwargs):
            resolver = _ledger_read_resolvers.get(symbol_name)
            if resolver is None:
                raise RuntimeError(f"ledger_read_accessors.{symbol_name} resolver not wired")
            return resolver(*args, **kwargs)

        resolve_ledger_read_symbol.__name__ = attribute_name
        globals()[attribute_name] = resolve_ledger_read_symbol
        return resolve_ledger_read_symbol
    raise AttributeError(attribute_name)

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
