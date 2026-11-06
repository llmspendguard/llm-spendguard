"""Dependency-free wiring for managed request cursor construction."""

_managed_request_cursor_factory = None


def set_managed_request_cursor_factory(factory):
    """Wire the managed request cursor implementation after it is defined."""
    global _managed_request_cursor_factory
    _managed_request_cursor_factory = factory


def make_managed_request_cursor_state(owner):
    """Construct managed request cursor state through the wired implementation."""
    if _managed_request_cursor_factory is None:
        raise RuntimeError("import spendguard before creating a managed request cursor state")
    return _managed_request_cursor_factory(owner)
