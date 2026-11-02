"""Busy managed-read expiry stays observable without breaking doctor silence."""
import contextlib
import io
import logging
import sys
import threading

from _pytest_fixture_bridge import align_test_startup_home, run_pytest_style

align_test_startup_home(prefix=__name__)

from spendguard import config
from spendguard.ledger_request_cursor import (
    MANAGED_READ_CLEANUP_DEFERRED,
    MANAGED_READ_EXPIRY_DEFERRED_LOG_MESSAGE,
    ManagedRequestCursorState,
)

DEFERRAL_SILENCE_LOGGER_NAME = config.__package__
DEFERRAL_SILENCE_MEMORY_TARGET = ":memory:"


def test_busy_managed_expiry_is_debug_observable_and_warning_silent(monkeypatch):
    doctor_logger = logging.getLogger(DEFERRAL_SILENCE_LOGGER_NAME)
    source_logger = logging.getLogger(ManagedRequestCursorState.__module__)
    monkeypatch.setattr(source_logger, "level", logging.DEBUG)
    warning_stream = io.StringIO()
    debug_stream = io.StringIO()
    warning_handler = logging.StreamHandler(warning_stream)
    debug_handler = logging.StreamHandler(debug_stream)
    warning_handler.setLevel(logging.WARNING)
    debug_handler.setLevel(logging.DEBUG)
    previous_level = doctor_logger.level
    doctor_logger.setLevel(logging.DEBUG)
    doctor_logger.addHandler(warning_handler)
    doctor_logger.addHandler(debug_handler)
    try:
        with contextlib.closing(config.open_ledger_connection(
                DEFERRAL_SILENCE_MEMORY_TARGET, tune=False)) as connection:
            # The production RLock is reentrant; a Lock models an occupied owner
            # synchronously, without starting a worker or registering a deadline.
            connection._ledger_request_step_lock = threading.Lock()
            with contextlib.closing(connection.cursor()) as cursor:
                state = ManagedRequestCursorState(cursor)
                assert state.step_lock is connection._ledger_request_step_lock
                with connection._ledger_request_step_lock:
                    outcome = state.release_idle_managed_request_cursor()
                assert outcome == state.expiry_cleanup_outcome == MANAGED_READ_CLEANUP_DEFERRED
                assert warning_stream.getvalue() == "", "doctor WARNING handler must remain silent"
                assert debug_stream.getvalue().splitlines() == [MANAGED_READ_EXPIRY_DEFERRED_LOG_MESSAGE]
    finally:
        doctor_logger.removeHandler(warning_handler)
        doctor_logger.removeHandler(debug_handler)
        doctor_logger.setLevel(previous_level)
        warning_handler.close()
        debug_handler.close()


if __name__ == "__main__":
    sys.exit(run_pytest_style(globals()))
