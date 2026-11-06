"""Dependency-free resolver bridge for cross-domain token-operations consumers."""

import threading

_token_operations_accessor_lock = threading.Lock()
_token_operations_resolvers = {}
_token_operations_loader_lock = threading.Lock()
_pending_loaders = []


def register_lazy_loader(loader):
    """Register a home loader to run once, immediately before the first unresolved access."""
    with _token_operations_loader_lock:
        _pending_loaders.append(loader)


def _run_pending_loaders():
    with _token_operations_loader_lock:
        loaders = tuple(_pending_loaders)
        _pending_loaders.clear()
    for loader in loaders:
        loader()


def __getattr__(attribute_name):
    """Materialize resolver APIs from their fixed ``set_``/``resolve_`` protocol."""
    if attribute_name.startswith("set_") and attribute_name.endswith("_resolver"):
        symbol_name = attribute_name[len("set_"):-len("_resolver")]

        def set_token_operations_symbol_resolver(resolver):
            with _token_operations_accessor_lock:
                _token_operations_resolvers[symbol_name] = resolver

        set_token_operations_symbol_resolver.__name__ = attribute_name
        globals()[attribute_name] = set_token_operations_symbol_resolver
        return set_token_operations_symbol_resolver
    if attribute_name.startswith("resolve_"):
        symbol_name = attribute_name[len("resolve_"):]

        def resolve_token_operations_symbol(*args, **kwargs):
            resolver = _token_operations_resolvers.get(symbol_name)
            if resolver is None:
                _run_pending_loaders()
                resolver = _token_operations_resolvers.get(symbol_name)
            if resolver is None:
                raise RuntimeError(f"token_operations_accessors.{symbol_name} resolver not wired")
            return resolver(*args, **kwargs)

        resolve_token_operations_symbol.__name__ = attribute_name
        globals()[attribute_name] = resolve_token_operations_symbol
        return resolve_token_operations_symbol
    raise AttributeError(attribute_name)
