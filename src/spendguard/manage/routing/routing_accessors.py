"""Dependency-free resolver bridge for cross-domain routing consumers."""

import threading

_routing_accessor_lock = threading.Lock()
_routing_resolvers = {}
_routing_loader_lock = threading.Lock()
_routing_pending_loaders = []


def register_routing_lazy_loader(loader):
    """Register a home loader to run once, immediately before the first unresolved access."""
    with _routing_loader_lock:
        _routing_pending_loaders.append(loader)


def _run_routing_pending_loaders():
    with _routing_loader_lock:
        loaders = tuple(_routing_pending_loaders)
        _routing_pending_loaders.clear()
    for loader in loaders:
        loader()


def __getattr__(attribute_name):
    """Materialize resolver APIs from their fixed ``set_``/``resolve_`` protocol."""
    if attribute_name.startswith("set_") and attribute_name.endswith("_resolver"):
        symbol_name = attribute_name[len("set_"):-len("_resolver")]

        def set_routing_symbol_resolver(resolver):
            with _routing_accessor_lock:
                _routing_resolvers[symbol_name] = resolver

        set_routing_symbol_resolver.__name__ = attribute_name
        globals()[attribute_name] = set_routing_symbol_resolver
        return set_routing_symbol_resolver
    if attribute_name.startswith("resolve_"):
        symbol_name = attribute_name[len("resolve_"):]

        def resolve_routing_symbol(*args, **kwargs):
            resolver = _routing_resolvers.get(symbol_name)
            if resolver is None:
                _run_routing_pending_loaders()
                resolver = _routing_resolvers.get(symbol_name)
            if resolver is None:
                raise RuntimeError(f"routing_accessors.{symbol_name} resolver not wired")
            return resolver(*args, **kwargs)

        resolve_routing_symbol.__name__ = attribute_name
        globals()[attribute_name] = resolve_routing_symbol
        return resolve_routing_symbol
    raise AttributeError(attribute_name)
