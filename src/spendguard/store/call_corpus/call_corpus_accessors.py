"""Dependency-free runtime accessors for the call corpus."""
import threading


_calls_insert_resolver = None
_calls_insert_resolver_lock = threading.Lock()


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
