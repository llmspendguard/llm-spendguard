"""Dependency-free runtime accessors that reference-data modules consume without importing their providers."""
import threading

import functools
import sys
import types


def _late_bind_reference_data_module_function(resolver):
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
    def call_current_reference_data_module_function(*args, **kwargs):
        resolver_module = sys.modules.get(resolver_module_name)
        current_resolver = getattr(resolver_module, resolver_function_name, None)
        if not callable(current_resolver):
            current_resolver = resolver
        return current_resolver(*args, **kwargs)

    return call_current_reference_data_module_function


_model_ceiling_resolver = None
_model_facts_resolver = None
_effort_discovery_resolver = None
_deliberate_stop_resolver = None
_reference_data_resolver_lock = threading.Lock()
_inbound_reference_data_resolver_lock = threading.Lock()
_inbound_reference_data_resolvers = {}


def __getattr__(attribute_name):
    """Materialize the generic inbound ``set_``/``resolve_`` callable protocol."""
    if attribute_name.startswith("set_") and attribute_name.endswith("_resolver"):
        symbol_name = attribute_name[len("set_"):-len("_resolver")]

        def set_inbound_reference_data_resolver(resolver):
            with _inbound_reference_data_resolver_lock:
                _inbound_reference_data_resolvers[symbol_name] = _late_bind_reference_data_module_function(resolver)

        set_inbound_reference_data_resolver.__name__ = attribute_name
        globals()[attribute_name] = set_inbound_reference_data_resolver
        return set_inbound_reference_data_resolver
    if attribute_name.startswith("resolve_"):
        symbol_name = attribute_name[len("resolve_"):]

        def resolve_inbound_reference_data_symbol(*args, **kwargs):
            with _inbound_reference_data_resolver_lock:
                resolver = _inbound_reference_data_resolvers.get(symbol_name)
            if resolver is None:
                raise RuntimeError(f"reference_data_accessors.{symbol_name} resolver not wired")
            return resolver(*args, **kwargs)

        resolve_inbound_reference_data_symbol.__name__ = attribute_name
        globals()[attribute_name] = resolve_inbound_reference_data_symbol
        return resolve_inbound_reference_data_symbol
    raise AttributeError(attribute_name)


def set_model_ceiling_resolver(model_ceiling_resolver):
    """Wire the resolver for live model output ceilings."""
    global _model_ceiling_resolver
    with _reference_data_resolver_lock:
        _model_ceiling_resolver = _late_bind_reference_data_module_function(model_ceiling_resolver)


def resolve_model_ceiling(vendor, rid):
    """Resolve a live model output ceiling through the eagerly wired provider."""
    with _reference_data_resolver_lock:
        model_ceiling_resolver = _model_ceiling_resolver
    if model_ceiling_resolver is None:
        raise RuntimeError("model ceiling resolver is not wired")
    return model_ceiling_resolver(vendor, rid)


def set_model_facts_resolver(model_facts_resolver):
    """Wire the resolver for learned model facts."""
    global _model_facts_resolver
    with _reference_data_resolver_lock:
        _model_facts_resolver = _late_bind_reference_data_module_function(model_facts_resolver)


def resolve_model_facts(model):
    """Resolve learned model facts through the eagerly wired provider."""
    with _reference_data_resolver_lock:
        model_facts_resolver = _model_facts_resolver
    if model_facts_resolver is None:
        raise RuntimeError("model facts resolver is not wired")
    return model_facts_resolver(model)


def set_deliberate_stop_resolver(deliberate_stop_resolver):
    """Wire the predicate for deliberate-stop exceptions."""
    global _deliberate_stop_resolver
    with _reference_data_resolver_lock:
        _deliberate_stop_resolver = _late_bind_reference_data_module_function(deliberate_stop_resolver)


def resolve_deliberate_stop(exc):
    """Identify a deliberate-stop exception through the eagerly wired provider."""
    with _reference_data_resolver_lock:
        deliberate_stop_resolver = _deliberate_stop_resolver
    if deliberate_stop_resolver is None:
        raise RuntimeError("deliberate stop resolver is not wired")
    return deliberate_stop_resolver(exc)


def set_effort_discovery_resolver(effort_discovery_resolver):
    """Wire the resolver for live reasoning-effort discovery."""
    global _effort_discovery_resolver
    with _reference_data_resolver_lock:
        _effort_discovery_resolver = _late_bind_reference_data_module_function(effort_discovery_resolver)


def resolve_effort_discovery(provider, model_id):
    """Discover accepted reasoning efforts through the eagerly wired provider."""
    with _reference_data_resolver_lock:
        effort_discovery_resolver = _effort_discovery_resolver
    if effort_discovery_resolver is None:
        raise RuntimeError("effort discovery resolver is not wired")
    return effort_discovery_resolver(provider, model_id)
