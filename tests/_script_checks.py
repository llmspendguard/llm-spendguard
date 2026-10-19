"""Shared exception injection and call observation for script-style tests."""

import asyncio
from functools import partial


def callable_raises_exception(fn, exc=Exception, *, unexpected_returns_false=False):
    """Observe the expected exception, preserving the caller's unrelated-error policy."""
    try:
        fn()
        return False
    except exc:
        return True
    except Exception:
        if unexpected_returns_false:
            return False
        raise


def raise_test_exception(*args, exception_factory, **kwargs):
    """Raise a fresh configured exception, ignoring the injected call's arguments."""
    raise exception_factory()


async def async_return_value(*args, return_value, **kwargs):
    """Return the configured sentinel from an async injected call."""
    return return_value


def execute_gate_test_call(w, is_async, **kw):
    """Invoke a gate wrapper with its script-test receiver."""
    return asyncio.run(w(None, **kw)) if is_async else w(None, **kw)


def raise_gate_test_error(*args, **kwargs):
    """Inject the gate tests' controlled implementation failure."""
    raise_test_exception(exception_factory=partial(ValueError, GATE_TEST_ERROR_MESSAGE))


GATE_TEST_ERROR_MESSAGE = "injected gate bug"
