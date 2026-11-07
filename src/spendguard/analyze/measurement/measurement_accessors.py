"""Dependency-free resolver bridge for cross-domain measurement consumers."""

import threading

_measurement_accessor_lock = threading.Lock()
_measurement_resolvers = {}
_measurement_loader_lock = threading.Lock()
_measurement_pending_loaders = []


def register_measurement_lazy_loader(loader):
    """Register a home loader to run once, immediately before the first unresolved access."""
    with _measurement_loader_lock:
        _measurement_pending_loaders.append(loader)


def _run_measurement_pending_loaders():
    with _measurement_loader_lock:
        loaders = tuple(_measurement_pending_loaders)
        _measurement_pending_loaders.clear()
    for loader in loaders:
        loader()


def __getattr__(attribute_name):
    """Materialize resolver APIs from their fixed ``set_``/``resolve_`` protocol."""
    if attribute_name.startswith("set_") and attribute_name.endswith("_resolver"):
        symbol_name = attribute_name[len("set_"):-len("_resolver")]

        def set_measurement_symbol_resolver(resolver):
            with _measurement_accessor_lock:
                _measurement_resolvers[symbol_name] = resolver

        set_measurement_symbol_resolver.__name__ = attribute_name
        globals()[attribute_name] = set_measurement_symbol_resolver
        return set_measurement_symbol_resolver
    if attribute_name.startswith("resolve_"):
        symbol_name = attribute_name[len("resolve_"):]

        def resolve_measurement_symbol(*args, **kwargs):
            resolver = _measurement_resolvers.get(symbol_name)
            if resolver is None:
                _run_measurement_pending_loaders()
                resolver = _measurement_resolvers.get(symbol_name)
            if resolver is None:
                raise RuntimeError(f"measurement_accessors.{symbol_name} resolver not wired")
            return resolver(*args, **kwargs)

        resolve_measurement_symbol.__name__ = attribute_name
        globals()[attribute_name] = resolve_measurement_symbol
        return resolve_measurement_symbol
    raise AttributeError(attribute_name)
