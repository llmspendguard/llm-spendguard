"""Dependency-free inbound seam for platform-domain symbols."""

import threading

import functools
import sys
import types


def _late_bind_platform_module_function(resolver):
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
    def call_current_platform_module_function(*args, **kwargs):
        resolver_module = sys.modules.get(resolver_module_name)
        current_resolver = getattr(resolver_module, resolver_function_name, None)
        if not callable(current_resolver):
            current_resolver = resolver
        return current_resolver(*args, **kwargs)

    return call_current_platform_module_function


CONSUMER_ROLE_DAEMON = "daemon"
CONSUMER_ROLE_EPHEMERAL = "ephemeral"
SUPERVISION_LAUNCHD = "launchd"
FLEET_RESTARTS_ENABLED_DEFAULT = True
FLEET_RESTARTS_ENABLED_ENV = "SPENDGUARD_FLEET_RESTARTS_ENABLED"
_SENTINEL = "spendguard-gate-hook/v1"
_PTH_SENTINEL = "spendguard-gate-hook/v2"

_platform_accessor_lock = threading.Lock()
_platform_resolvers = {}


def __getattr__(attribute_name):
    """Materialize resolver APIs from their fixed ``set_``/``resolve_`` protocol."""
    if attribute_name.startswith("set_") and attribute_name.endswith("_resolver"):
        symbol_name = attribute_name[len("set_"):-len("_resolver")]

        def set_platform_symbol_resolver(resolver):
            with _platform_accessor_lock:
                _platform_resolvers[symbol_name] = _late_bind_platform_module_function(resolver)

        set_platform_symbol_resolver.__name__ = attribute_name
        globals()[attribute_name] = set_platform_symbol_resolver
        return set_platform_symbol_resolver
    if attribute_name.startswith("resolve_"):
        symbol_name = attribute_name[len("resolve_"):]

        def resolve_platform_symbol(*args, **kwargs):
            resolver = _platform_resolvers.get(symbol_name)
            if resolver is None:
                raise RuntimeError(f"platform_accessors.{symbol_name} resolver not wired")
            return resolver(*args, **kwargs)

        resolve_platform_symbol.__name__ = attribute_name
        globals()[attribute_name] = resolve_platform_symbol
        return resolve_platform_symbol
    raise AttributeError(attribute_name)
