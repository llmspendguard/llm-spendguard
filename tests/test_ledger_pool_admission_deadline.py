"""Aggregate deadlines include a saturated read pool's admission wait."""
import sys
import threading
import time
from unittest.mock import patch

from _pytest_fixture_bridge import align_test_startup_home, run_pytest_style

align_test_startup_home(prefix=__name__)

from spendguard import config, ledger, ledger_read

import pytest

ADMISSION_REPRO_CAP = 1
ADMISSION_REPRO_DEADLINE_S = 0.01
ADMISSION_REPRO_HOLD_MULTIPLIER = 40
ADMISSION_REPRO_SCHEDULING_MULTIPLIER = 10


def test_ambient_scope_refuses_deadlineless_read_pool_admission():
    current = ledger.SpendLedger()
    for accessor in (lambda: current.get(__name__), lambda: current.history(__name__),
                     current.verify_audit_chain):
        assert_ambient_pool_refusal(current, accessor)


def test_ambient_scope_refuses_direct_read_pool_admission():
    current = ledger.SpendLedger()

    def borrow_ambient_direct_pool():
        with config.managed_ledger_read_connection(current.db_path):
            pass

    assert_ambient_pool_refusal(current, borrow_ambient_direct_pool)


def assert_ambient_pool_refusal(current, accessor):
    occupied = threading.Event()
    release = threading.Event()
    hold_s = ADMISSION_REPRO_DEADLINE_S * ADMISSION_REPRO_HOLD_MULTIPLIER

    def occupy_ambient_deadline_pool():
        with config.managed_ledger_read_connection(current.db_path):
            occupied.set()
            release.wait(timeout=hold_s)

    with patch.object(config, "ledger_read_pool_settings", return_value=(ADMISSION_REPRO_CAP, hold_s)):
        holder = threading.Thread(target=occupy_ambient_deadline_pool)
        holder.start()
        try:
            assert occupied.wait(timeout=config.LEDGER_WRITE_TIMEOUT_S)
            started = time.monotonic()
            with pytest.raises(ledger_read.LedgerQueryDeadline):
                with ledger_read.ledger_request_scope(deadline_s=ADMISSION_REPRO_DEADLINE_S):
                    accessor()
            assert time.monotonic() - started < (
                ADMISSION_REPRO_DEADLINE_S * ADMISSION_REPRO_SCHEDULING_MULTIPLIER)
            assert holder.is_alive(), "scope expiry must precede holder release"
        finally:
            release.set()
            holder.join(timeout=config.LEDGER_WRITE_TIMEOUT_S)
        assert not holder.is_alive()


def test_aggregate_deadline_expires_before_pool_release():
    current = ledger.SpendLedger()
    hold_s = ADMISSION_REPRO_DEADLINE_S * ADMISSION_REPRO_HOLD_MULTIPLIER
    for accessor in (current.spent_dec, current.running_llm_total_dec):
        occupied = threading.Event()
        release = threading.Event()

        def occupy_aggregate_deadline_pool(occupied=occupied, release=release):
            with config.managed_ledger_read_connection(current.db_path):
                occupied.set()
                release.wait(timeout=hold_s)

        with patch.object(config, "ledger_read_pool_settings", return_value=(ADMISSION_REPRO_CAP, hold_s)):
            holder = threading.Thread(target=occupy_aggregate_deadline_pool)
            holder.start()
            try:
                assert occupied.wait(timeout=config.LEDGER_WRITE_TIMEOUT_S)
                started = time.monotonic()
                try:
                    accessor(deadline_s=ADMISSION_REPRO_DEADLINE_S)
                except ledger_read.LedgerQueryDeadline:
                    pass
                else:
                    raise AssertionError("saturated admission must raise LedgerQueryDeadline")
                assert time.monotonic() - started < (
                    ADMISSION_REPRO_DEADLINE_S * ADMISSION_REPRO_SCHEDULING_MULTIPLIER)
                assert holder.is_alive(), "timeout must precede lease release"
            finally:
                release.set()
                holder.join(timeout=config.LEDGER_WRITE_TIMEOUT_S)
            assert not holder.is_alive()


if __name__ == "__main__":
    sys.exit(run_pytest_style(globals()))
