"""Dependency-free resolver bridge for cross-domain SaaS consumers."""

import threading

_saas_accessor_lock = threading.Lock()
_saas_resolvers = {}


def __getattr__(attribute_name):
    """Materialize resolver APIs from their fixed ``set_``/``resolve_`` protocol."""
    if attribute_name.startswith("set_") and attribute_name.endswith("_resolver"):
        symbol_name = attribute_name[len("set_"):-len("_resolver")]

        def set_saas_symbol_resolver(resolver):
            with _saas_accessor_lock:
                _saas_resolvers[symbol_name] = resolver

        set_saas_symbol_resolver.__name__ = attribute_name
        globals()[attribute_name] = set_saas_symbol_resolver
        return set_saas_symbol_resolver
    if attribute_name.startswith("resolve_"):
        symbol_name = attribute_name[len("resolve_"):]

        def resolve_saas_symbol(*args, **kwargs):
            resolver = _saas_resolvers.get(symbol_name)
            if resolver is None:
                raise RuntimeError(f"saas_accessors.{symbol_name} resolver not wired")
            return resolver(*args, **kwargs)

        resolve_saas_symbol.__name__ = attribute_name
        globals()[attribute_name] = resolve_saas_symbol
        return resolve_saas_symbol
    raise AttributeError(attribute_name)
