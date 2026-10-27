"""An expired reservation never becomes a late committed admission."""
import sys
import time
import threading
import fcntl
from pathlib import Path

import pytest
from _pytest_fixture_bridge import align_test_startup_home, run_pytest_style

align_test_startup_home(prefix=__name__)

from spendguard import config, ledger_writer, xp_rate_window
from spendguard.ledger_read import ledger_request_scope

RATE_REPRO_BUDGET_S = 0.02
RATE_REPRO_HOLD_FACTOR = 20
RATE_REPRO_SCHEDULING_FACTOR = 5
RATE_REPRO_LIMITS = [(config.LEDGER_WRITE_TIMEOUT_S, 1, None)]


def test_rate_window_sleep_inherits_ambient_request_expiry(monkeypatch):
    def reject_ambient_rate_capacity(*args):
        return False, RATE_REPRO_BUDGET_S * RATE_REPRO_HOLD_FACTOR

    monkeypatch.setattr(xp_rate_window, "_commit_rate_window_reservation", reject_ambient_rate_capacity)
    started = time.monotonic()
    with ledger_request_scope(deadline_s=RATE_REPRO_BUDGET_S):
        with pytest.raises(xp_rate_window.RateDeadline):
            xp_rate_window.reserve(test_rate_window_sleep_inherits_ambient_request_expiry.__name__,
                                   RATE_REPRO_LIMITS,
                                   deadline_s=RATE_REPRO_BUDGET_S * RATE_REPRO_HOLD_FACTOR)
    assert time.monotonic() - started < RATE_REPRO_BUDGET_S * RATE_REPRO_SCHEDULING_FACTOR


def test_rate_reservation_execution_expiry_rolls_back(monkeypatch):
    original = xp_rate_window._try_reserve
    release = threading.Event()

    def delayed_rate_reservation_attempt(*args):
        release.wait(timeout=RATE_REPRO_BUDGET_S * RATE_REPRO_HOLD_FACTOR)
        return original(*args)

    monkeypatch.setattr(xp_rate_window, "_try_reserve", delayed_rate_reservation_attempt)
    started = time.monotonic()
    try:
        with pytest.raises(xp_rate_window.RateDeadline):
            xp_rate_window.reserve(__name__, RATE_REPRO_LIMITS, deadline_s=RATE_REPRO_BUDGET_S)
        assert time.monotonic() - started < RATE_REPRO_BUDGET_S * RATE_REPRO_SCHEDULING_FACTOR
    finally:
        release.set()
    count = ledger_writer.run_ledger_write(lambda: ledger_writer.current_ledger_writer_connection().execute(
        "SELECT COUNT(*) FROM xp_rate WHERE k=?", (__name__,)).fetchone()[0])
    assert count == 0


def test_rate_reservation_admission_expires_before_writer_release():
    occupied = threading.Event()
    release = threading.Event()

    def occupy_rate_reservation_writer():
        occupied.set()
        release.wait(timeout=RATE_REPRO_BUDGET_S * RATE_REPRO_HOLD_FACTOR)

    holder = threading.Thread(target=lambda: ledger_writer.run_ledger_write(occupy_rate_reservation_writer))
    holder.start()
    try:
        assert occupied.wait(timeout=config.LEDGER_WRITE_TIMEOUT_S)
        started = time.monotonic()
        with pytest.raises(xp_rate_window.RateDeadline):
            xp_rate_window.reserve(test_rate_reservation_admission_expires_before_writer_release.__name__,
                                   RATE_REPRO_LIMITS, deadline_s=RATE_REPRO_BUDGET_S)
        assert time.monotonic() - started < RATE_REPRO_BUDGET_S * RATE_REPRO_SCHEDULING_FACTOR
        assert holder.is_alive()
    finally:
        release.set()
        holder.join(timeout=config.LEDGER_WRITE_TIMEOUT_S)
    assert not holder.is_alive()


def test_rate_reservation_os_lock_expiry_never_commits():
    ledger_writer.run_ledger_write(lambda: None)
    occupied = threading.Event()
    release = threading.Event()
    lock_path = Path(config.db_path() + ledger_writer.LEDGER_WRITER_LOCK_SUFFIX)

    def hold_rate_reservation_os_lock():
        with lock_path.open("a") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            occupied.set()
            release.wait(timeout=RATE_REPRO_BUDGET_S * RATE_REPRO_HOLD_FACTOR)
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    holder = threading.Thread(target=hold_rate_reservation_os_lock)
    holder.start()
    try:
        assert occupied.wait(timeout=config.LEDGER_WRITE_TIMEOUT_S)
        started = time.monotonic()
        with pytest.raises(xp_rate_window.RateDeadline):
            xp_rate_window.reserve(test_rate_reservation_os_lock_expiry_never_commits.__name__,
                                   RATE_REPRO_LIMITS, deadline_s=RATE_REPRO_BUDGET_S)
        assert time.monotonic() - started < RATE_REPRO_BUDGET_S * RATE_REPRO_SCHEDULING_FACTOR
        assert holder.is_alive()
    finally:
        release.set()
        holder.join(timeout=config.LEDGER_WRITE_TIMEOUT_S)
    assert not holder.is_alive()
    count = ledger_writer.run_ledger_write(lambda: ledger_writer.current_ledger_writer_connection().execute(
        "SELECT COUNT(*) FROM xp_rate WHERE k=?",
        (test_rate_reservation_os_lock_expiry_never_commits.__name__,)).fetchone()[0])
    assert count == 0


if __name__ == "__main__":
    sys.exit(run_pytest_style(globals()))
