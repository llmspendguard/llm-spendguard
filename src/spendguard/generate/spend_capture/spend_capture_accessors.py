"""Dependency-free resolver bridge for cross-domain spend-capture consumers."""

import threading

_spend_capture_accessor_lock = threading.Lock()
_spend_capture_resolvers = {}
_spend_capture_loader_lock = threading.Lock()
_spend_capture_pending_loaders = []


def register_spend_capture_lazy_loader(loader):
    """Register a home loader to run once, immediately before the first unresolved access."""
    with _spend_capture_loader_lock:
        _spend_capture_pending_loaders.append(loader)


def _run_spend_capture_pending_loaders():
    with _spend_capture_loader_lock:
        loaders = tuple(_spend_capture_pending_loaders)
        _spend_capture_pending_loaders.clear()
    for loader in loaders:
        loader()


def __getattr__(attribute_name):
    """Materialize resolver APIs from their fixed ``set_``/``resolve_`` protocol."""
    if attribute_name.startswith("set_") and attribute_name.endswith("_resolver"):
        symbol_name = attribute_name[len("set_"):-len("_resolver")]

        def set_spend_capture_symbol_resolver(resolver):
            with _spend_capture_accessor_lock:
                _spend_capture_resolvers[symbol_name] = resolver

        set_spend_capture_symbol_resolver.__name__ = attribute_name
        globals()[attribute_name] = set_spend_capture_symbol_resolver
        return set_spend_capture_symbol_resolver
    if attribute_name.startswith("resolve_"):
        symbol_name = attribute_name[len("resolve_"):]

        def resolve_spend_capture_symbol(*args, **kwargs):
            resolver = _spend_capture_resolvers.get(symbol_name)
            if resolver is None:
                _run_spend_capture_pending_loaders()
                resolver = _spend_capture_resolvers.get(symbol_name)
            if resolver is None:
                raise RuntimeError(f"spend_capture_accessors.{symbol_name} resolver not wired")
            return resolver(*args, **kwargs)

        resolve_spend_capture_symbol.__name__ = attribute_name
        globals()[attribute_name] = resolve_spend_capture_symbol
        return resolve_spend_capture_symbol
    raise AttributeError(attribute_name)
