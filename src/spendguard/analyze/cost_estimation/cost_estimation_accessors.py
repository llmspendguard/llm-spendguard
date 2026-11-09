"""Dependency-free runtime accessors for bulk cost-estimation resolvers."""
import threading

import functools
import sys
import types


def _late_bind_cost_estimation_module_function(resolver):
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
    def call_current_cost_estimation_module_function(*args, **kwargs):
        resolver_module = sys.modules.get(resolver_module_name)
        current_resolver = getattr(resolver_module, resolver_function_name, None)
        if not callable(current_resolver):
            current_resolver = resolver
        return current_resolver(*args, **kwargs)

    return call_current_cost_estimation_module_function


_bulk_maxtokens_resolver = None
_bulk_model_outputs_resolver = None
_bulk_sig_resolver = None
_bulk_reasoning_out_estimate_resolver = None
_cost_estimation_resolver_lock = threading.Lock()


def set_bulk_maxtokens_resolver(bulk_maxtokens_resolver):
    """Wire the resolver for bulk maximum-token measurements."""
    global _bulk_maxtokens_resolver
    with _cost_estimation_resolver_lock:
        _bulk_maxtokens_resolver = _late_bind_cost_estimation_module_function(bulk_maxtokens_resolver)


def resolve_bulk_maxtokens(sig, *args, **kwargs):
    """Resolve bulk maximum-token measurements through the eagerly wired provider."""
    with _cost_estimation_resolver_lock:
        bulk_maxtokens_resolver = _bulk_maxtokens_resolver
    if bulk_maxtokens_resolver is None:
        raise RuntimeError("bulk maximum-token resolver is not wired")
    return bulk_maxtokens_resolver(sig, *args, **kwargs)


def set_bulk_model_outputs_resolver(bulk_model_outputs_resolver):
    """Wire the resolver for bulk model-output measurements."""
    global _bulk_model_outputs_resolver
    with _cost_estimation_resolver_lock:
        _bulk_model_outputs_resolver = _late_bind_cost_estimation_module_function(bulk_model_outputs_resolver)


def resolve_bulk_model_outputs(model, *args, **kwargs):
    """Resolve bulk model-output measurements through the eagerly wired provider."""
    with _cost_estimation_resolver_lock:
        bulk_model_outputs_resolver = _bulk_model_outputs_resolver
    if bulk_model_outputs_resolver is None:
        raise RuntimeError("bulk model-output resolver is not wired")
    return bulk_model_outputs_resolver(model, *args, **kwargs)


def set_bulk_sig_resolver(bulk_sig_resolver):
    """Wire the resolver for bulk call signatures."""
    global _bulk_sig_resolver
    with _cost_estimation_resolver_lock:
        _bulk_sig_resolver = _late_bind_cost_estimation_module_function(bulk_sig_resolver)


def resolve_bulk_sig(model, template_id=None, *args, **kwargs):
    """Resolve a bulk call signature through the eagerly wired provider."""
    with _cost_estimation_resolver_lock:
        bulk_sig_resolver = _bulk_sig_resolver
    if bulk_sig_resolver is None:
        raise RuntimeError("bulk signature resolver is not wired")
    return bulk_sig_resolver(model, *args, template_id=template_id, **kwargs)


def set_bulk_reasoning_out_estimate_resolver(bulk_reasoning_out_estimate_resolver):
    """Wire the resolver for bulk reasoning-output estimates."""
    global _bulk_reasoning_out_estimate_resolver
    with _cost_estimation_resolver_lock:
        _bulk_reasoning_out_estimate_resolver = _late_bind_cost_estimation_module_function(bulk_reasoning_out_estimate_resolver)


def resolve_bulk_reasoning_out_estimate(*args, **kwargs):
    """Resolve a bulk reasoning-output estimate through the eagerly wired provider."""
    with _cost_estimation_resolver_lock:
        bulk_reasoning_out_estimate_resolver = _bulk_reasoning_out_estimate_resolver
    if bulk_reasoning_out_estimate_resolver is None:
        raise RuntimeError("bulk reasoning-output estimate resolver is not wired")
    return bulk_reasoning_out_estimate_resolver(*args, **kwargs)
