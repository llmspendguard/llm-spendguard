"""Dependency-free resolver bridge for cross-domain reconciliation consumers."""

import threading

_reconciliation_accessor_lock = threading.Lock()
_reconciliation_resolvers = {}
_reconciliation_loader_lock = threading.Lock()
_reconciliation_pending_loaders = []


def register_reconciliation_lazy_loader(loader):
    """Register a home loader to run once, immediately before the first unresolved access."""
    with _reconciliation_loader_lock:
        _reconciliation_pending_loaders.append(loader)


def _run_reconciliation_pending_loaders():
    with _reconciliation_loader_lock:
        loaders = tuple(_reconciliation_pending_loaders)
        _reconciliation_pending_loaders.clear()
    for loader in loaders:
        loader()


def __getattr__(attribute_name):
    """Materialize resolver APIs from their fixed ``set_``/``resolve_`` protocol."""
    if attribute_name.startswith("set_") and attribute_name.endswith("_resolver"):
        symbol_name = attribute_name[len("set_"):-len("_resolver")]

        def set_reconciliation_symbol_resolver(resolver):
            with _reconciliation_accessor_lock:
                _reconciliation_resolvers[symbol_name] = resolver

        set_reconciliation_symbol_resolver.__name__ = attribute_name
        globals()[attribute_name] = set_reconciliation_symbol_resolver
        return set_reconciliation_symbol_resolver
    if attribute_name.startswith("resolve_"):
        symbol_name = attribute_name[len("resolve_"):]

        def resolve_reconciliation_symbol(*args, **kwargs):
            resolver = _reconciliation_resolvers.get(symbol_name)
            if resolver is None:
                _run_reconciliation_pending_loaders()
                resolver = _reconciliation_resolvers.get(symbol_name)
            if resolver is None:
                raise RuntimeError(f"reconciliation_accessors.{symbol_name} resolver not wired")
            return resolver(*args, **kwargs)

        resolve_reconciliation_symbol.__name__ = attribute_name
        globals()[attribute_name] = resolve_reconciliation_symbol
        return resolve_reconciliation_symbol
    raise AttributeError(attribute_name)
