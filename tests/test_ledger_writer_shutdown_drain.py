"""Bounded shutdown coverage for a ledger writer stuck in an admitted callback."""
import contextlib
import faulthandler
import io
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
from unittest.mock import patch

from spendguard import config, ledger, ledger_read, ledger_writer


SHUTDOWN_DRAIN_PROBE_ARGUMENT = "--shutdown-drain-probe"
SHUTDOWN_DRAIN_TIMEOUT_DIVISOR = 100
SHUTDOWN_DRAIN_WALL_CLOCK_MULTIPLIER = 3
SHUTDOWN_DRAIN_SUBPROCESS_WATCHDOG_MULTIPLIER = 5
SHUTDOWN_DRAIN_EXPECTED_QUEUED_JOBS = 1
SHUTDOWN_DRAIN_TEST_ISOLATION_VALUE = "1"
SHUTDOWN_DRAIN_DATABASE_EXTENSION = ".sqlite"
SHUTDOWN_DRAIN_WARNING_FRAGMENT = "queued job(s) remain undrained"


def exercise_stuck_admitted_write_shutdown_drain():
    configured_shutdown_timeout_s = (
        config.LEDGER_POOL_IDLE_TIMEOUT_DEFAULT_S / SHUTDOWN_DRAIN_TIMEOUT_DIVISOR)
    original_ledger_read_pool_settings = config.ledger_read_pool_settings
    faulthandler.dump_traceback_later(
        configured_shutdown_timeout_s * SHUTDOWN_DRAIN_SUBPROCESS_WATCHDOG_MULTIPLIER,
        exit=True,
    )

    def shutdown_drain_probe_pool_settings():
        read_pool_cap, _ = original_ledger_read_pool_settings()
        return read_pool_cap, configured_shutdown_timeout_s

    with tempfile.TemporaryDirectory() as isolated_directory, \
            patch.object(config, "ledger_read_pool_settings", shutdown_drain_probe_pool_settings):
        stuck_owner_path = Path(isolated_directory) / f"{__name__}{SHUTDOWN_DRAIN_DATABASE_EXTENSION}"
        ledger.SpendLedger(stuck_owner_path)
        callback_started = threading.Event()
        callback_never_released = threading.Event()

        def block_admitted_shutdown_write_forever():
            callback_started.set()
            callback_never_released.wait()

        admitted_write_failures = []

        def expire_caller_after_admitting_stuck_write():
            try:
                with ledger_read.ledger_request_scope(deadline_s=configured_shutdown_timeout_s):
                    ledger_writer.run_ledger_write(block_admitted_shutdown_write_forever, stuck_owner_path)
            except ledger_read.LedgerQueryDeadline:
                pass
            except BaseException as error:
                admitted_write_failures.append(error)

        admitted_write_caller = threading.Thread(target=expire_caller_after_admitting_stuck_write)
        admitted_write_caller.start()
        assert callback_started.wait(timeout=config.LEDGER_WRITE_TIMEOUT_S)
        admitted_write_caller.join(timeout=config.LEDGER_WRITE_TIMEOUT_S)
        assert not admitted_write_caller.is_alive()
        assert not admitted_write_failures, admitted_write_failures

        warning_output = io.StringIO()
        shutdown_started_at = time.monotonic()
        with contextlib.redirect_stderr(warning_output):
            ledger_writer.close_ledger_writers()
        shutdown_elapsed_s = time.monotonic() - shutdown_started_at

        shutdown_wall_clock_bound_s = (
            configured_shutdown_timeout_s * SHUTDOWN_DRAIN_WALL_CLOCK_MULTIPLIER)
        assert shutdown_elapsed_s < shutdown_wall_clock_bound_s, shutdown_elapsed_s
        surfaced_warning = warning_output.getvalue()
        assert str(stuck_owner_path.resolve()) in surfaced_warning, surfaced_warning
        assert f"{SHUTDOWN_DRAIN_EXPECTED_QUEUED_JOBS} queued job(s)" in surfaced_warning, surfaced_warning
        print(surfaced_warning, end="", file=sys.stderr)


def test_stuck_admitted_write_shutdown_drain_is_bounded_and_surfaced():
    environment = dict(
        os.environ,
        SPENDGUARD_TEST_ISOLATED=SHUTDOWN_DRAIN_TEST_ISOLATION_VALUE,
    )
    subprocess_watchdog_s = (
        config.LEDGER_POOL_IDLE_TIMEOUT_DEFAULT_S
        / SHUTDOWN_DRAIN_TIMEOUT_DIVISOR
        * SHUTDOWN_DRAIN_SUBPROCESS_WATCHDOG_MULTIPLIER
    )
    try:
        completed = subprocess.run(
            [sys.executable, str(Path(__file__).resolve()), SHUTDOWN_DRAIN_PROBE_ARGUMENT],
            env=environment,
            capture_output=True,
            text=True,
            timeout=subprocess_watchdog_s,
        )
    except subprocess.TimeoutExpired as error:
        raise AssertionError("ledger writer shutdown drain exceeded its SSOT-derived watchdog") from error
    assert completed.returncode == 0, (completed.stdout, completed.stderr)
    assert SHUTDOWN_DRAIN_WARNING_FRAGMENT in completed.stderr, completed.stderr


if __name__ == "__main__":
    if SHUTDOWN_DRAIN_PROBE_ARGUMENT in sys.argv:
        exercise_stuck_admitted_write_shutdown_drain()
        sys.stdout.flush()
        sys.stderr.flush()
        os._exit(os.EX_OK)
    else:
        test_stuck_admitted_write_shutdown_drain_is_bounded_and_surfaced()
