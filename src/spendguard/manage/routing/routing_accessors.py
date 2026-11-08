"""Dependency-free resolver bridge for cross-domain routing consumers."""

import functools
import sys
import threading
import types

_routing_accessor_lock = threading.Lock()
_routing_resolvers = {}
_routing_loader_lock = threading.Lock()
_routing_pending_loaders = []


def _late_bind_routing_module_function(resolver):
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
    def call_current_routing_module_function(*args, **kwargs):
        resolver_module = sys.modules.get(resolver_module_name)
        current_resolver = getattr(resolver_module, resolver_function_name, None)
        if not callable(current_resolver):
            current_resolver = resolver
        return current_resolver(*args, **kwargs)

    return call_current_routing_module_function


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
                _routing_resolvers[symbol_name] = _late_bind_routing_module_function(resolver)

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
