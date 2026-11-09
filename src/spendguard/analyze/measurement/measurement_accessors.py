"""Dependency-free resolver bridge for cross-domain measurement consumers."""

import threading

import functools
import sys
import types


def _late_bind_measurement_module_function(resolver):
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
    def call_current_measurement_module_function(*args, **kwargs):
        resolver_module = sys.modules.get(resolver_module_name)
        current_resolver = getattr(resolver_module, resolver_function_name, None)
        if not callable(current_resolver):
            current_resolver = resolver
        return current_resolver(*args, **kwargs)

    return call_current_measurement_module_function

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
                _measurement_resolvers[symbol_name] = _late_bind_measurement_module_function(resolver)

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
