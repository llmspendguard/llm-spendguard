"""Neutral leaf for the two ledger-write decorators.

The real write machinery (``run_ledger_write``) and the warn/deliberate-stop
helpers are injected by ``ledger_writer`` at import time.  This module therefore
imports nothing from spendguard and cannot be pulled into the configuration
import cycle.
"""
import functools


_run_ledger_write_provider = None
_decorator_warn_once_provider = None
_decorator_is_deliberate_stop_provider = None


def set_run_ledger_write_provider(provider):
    """Set the run-ledger-write provider; the last writer wins."""
    global _run_ledger_write_provider
    _run_ledger_write_provider = provider


def set_decorator_warn_once_provider(provider):
    """Set the decorator warning provider; the last writer wins."""
    global _decorator_warn_once_provider
    _decorator_warn_once_provider = provider


def set_decorator_is_deliberate_stop_provider(provider):
    """Set the deliberate-stop provider; the last writer wins."""
    global _decorator_is_deliberate_stop_provider
    _decorator_is_deliberate_stop_provider = provider


def _resolve_run_ledger_write():
    if _run_ledger_write_provider is None:
        raise RuntimeError(
            "ledger_write_decorators run-ledger-write provider is not set; "
            "import spendguard (which imports ledger_writer, the wirer) before "
            "using the ledger-write decorators."
        )
    return _run_ledger_write_provider


def _resolve_decorator_warn_once():
    if _decorator_warn_once_provider is None:
        raise RuntimeError(
            "ledger_write_decorators decorator-warn-once provider is not set; "
            "import spendguard (which imports ledger_writer, the wirer) before "
            "using the ledger-write decorators."
        )
    return _decorator_warn_once_provider


def _resolve_decorator_is_deliberate_stop():
    if _decorator_is_deliberate_stop_provider is None:
        raise RuntimeError(
            "ledger_write_decorators deliberate-stop provider is not set; "
            "import spendguard (which imports ledger_writer, the wirer) before "
            "using the ledger-write decorators."
        )
    return _decorator_is_deliberate_stop_provider


def serialize_ledger_function(function):
    """Route an existing mutation entry point without changing its public API."""
    @functools.wraps(function)
    def invoke_serialized_ledger_function(*args, **kwargs):
        return _resolve_run_ledger_write()(lambda: function(*args, **kwargs))
    return invoke_serialized_ledger_function


def best_effort_ledger_function(function=None, *, failure_result=None):
    """Keep optional writer startup and dispatch inside a never-raises boundary.

    Required mutations must use serialize_ledger_function instead. The recorder
    body retains its normal return and warning behavior.
    """
    if function is None:
        return functools.partial(best_effort_ledger_function, failure_result=failure_result)

    @functools.wraps(function)
    def invoke_best_effort_ledger_function(*args, **kwargs):
        try:
            return _resolve_run_ledger_write()(lambda: function(*args, **kwargs))
        except Exception as bookkeeping_error:
            if _resolve_decorator_is_deliberate_stop()(bookkeeping_error):
                raise
            try:
                _resolve_decorator_warn_once()(
                    f"[spendguard] {function.__module__}.{function.__name__} write failed "
                    f"({type(bookkeeping_error).__name__}) — optional bookkeeping was not recorded")
            except Exception:
                pass
            return failure_result
    return invoke_best_effort_ledger_function
