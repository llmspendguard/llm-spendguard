"""Dependency-free runtime accessors for the call corpus."""
import threading


_calls_insert_resolver = None
_calls_insert_resolver_lock = threading.Lock()
_calls_cost_summary_resolver = None
_calls_cost_summary_resolver_lock = threading.Lock()
_calls_record_call_resolver = None
_calls_record_call_resolver_lock = threading.Lock()


def set_calls_insert_resolver(calls_insert_resolver):
    """Wire the resolver for inserting call-corpus rows."""
    global _calls_insert_resolver
    with _calls_insert_resolver_lock:
        _calls_insert_resolver = calls_insert_resolver


def resolve_calls_insert(*args, **kwargs):
    """Insert a call-corpus row through the eagerly wired provider."""
    with _calls_insert_resolver_lock:
        calls_insert_resolver = _calls_insert_resolver
    if calls_insert_resolver is None:
        raise RuntimeError("calls insert resolver is not wired")
    return calls_insert_resolver(*args, **kwargs)


def set_calls_cost_summary_resolver(calls_cost_summary_resolver):
    """Wire the resolver for summarizing call-corpus costs."""
    global _calls_cost_summary_resolver
    with _calls_cost_summary_resolver_lock:
        _calls_cost_summary_resolver = calls_cost_summary_resolver


def resolve_calls_cost_summary(*args, **kwargs):
    """Summarize call-corpus costs through the eagerly wired provider."""
    with _calls_cost_summary_resolver_lock:
        calls_cost_summary_resolver = _calls_cost_summary_resolver
    if calls_cost_summary_resolver is None:
        raise RuntimeError("calls cost summary resolver is not wired")
    return calls_cost_summary_resolver(*args, **kwargs)


def set_calls_record_call_resolver(calls_record_call_resolver):
    """Wire the resolver for recording call-corpus rows."""
    global _calls_record_call_resolver
    with _calls_record_call_resolver_lock:
        _calls_record_call_resolver = calls_record_call_resolver


def resolve_calls_record_call(*args, **kwargs):
    """Record a call-corpus row through the eagerly wired provider."""
    with _calls_record_call_resolver_lock:
        calls_record_call_resolver = _calls_record_call_resolver
    if calls_record_call_resolver is None:
        raise RuntimeError("calls record call resolver is not wired")
    return calls_record_call_resolver(*args, **kwargs)
