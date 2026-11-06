"""Dependency-free resolver bridge for cross-domain spend-reporting consumers."""

import threading

_spend_reporting_accessor_lock = threading.Lock()
_spend_reporting_resolvers = {}


def __getattr__(attribute_name):
    """Materialize resolver APIs from their fixed ``set_``/``resolve_`` protocol."""
    if attribute_name.startswith("set_") and attribute_name.endswith("_resolver"):
        symbol_name = attribute_name[len("set_"):-len("_resolver")]

        def set_spend_reporting_symbol_resolver(resolver):
            with _spend_reporting_accessor_lock:
                _spend_reporting_resolvers[symbol_name] = resolver

        set_spend_reporting_symbol_resolver.__name__ = attribute_name
        globals()[attribute_name] = set_spend_reporting_symbol_resolver
        return set_spend_reporting_symbol_resolver
    if attribute_name.startswith("resolve_"):
        symbol_name = attribute_name[len("resolve_"):]

        def resolve_spend_reporting_symbol(*args, **kwargs):
            resolver = _spend_reporting_resolvers.get(symbol_name)
            if resolver is None:
                raise RuntimeError(f"spend_reporting_accessors.{symbol_name} resolver not wired")
            return resolver(*args, **kwargs)

        resolve_spend_reporting_symbol.__name__ = attribute_name
        globals()[attribute_name] = resolve_spend_reporting_symbol
        return resolve_spend_reporting_symbol
    raise AttributeError(attribute_name)
