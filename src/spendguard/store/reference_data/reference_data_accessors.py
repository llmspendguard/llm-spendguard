"""Dependency-free runtime accessors for reference-data resolvers."""
import threading


_model_ceiling_resolver = None
_model_facts_resolver = None
_reference_data_resolver_lock = threading.Lock()


def set_model_ceiling_resolver(model_ceiling_resolver):
    """Wire the resolver for live model output ceilings."""
    global _model_ceiling_resolver
    with _reference_data_resolver_lock:
        _model_ceiling_resolver = model_ceiling_resolver


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
        _model_facts_resolver = model_facts_resolver


def resolve_model_facts(model):
    """Resolve learned model facts through the eagerly wired provider."""
    with _reference_data_resolver_lock:
        model_facts_resolver = _model_facts_resolver
    if model_facts_resolver is None:
        raise RuntimeError("model facts resolver is not wired")
    return model_facts_resolver(model)
