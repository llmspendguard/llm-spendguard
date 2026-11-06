"""Dependency-free inbound seam for platform-domain symbols."""

import threading


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
                _platform_resolvers[symbol_name] = resolver

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
