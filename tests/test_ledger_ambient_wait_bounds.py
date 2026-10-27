"""Every ledger admission must stop while a conflicting owner still holds it."""
import contextlib
import sys
import threading
import time
from unittest.mock import patch

import pytest
from _pytest_fixture_bridge import align_test_startup_home, run_pytest_style

align_test_startup_home(prefix=__name__)

from spendguard import config, ledger_read, ledger_wal_maintenance

AMBIENT_WAIT_SCOPE_S = 0.02
AMBIENT_WAIT_HOLD_FACTOR = 30
AMBIENT_WAIT_SCHEDULING_FACTOR = 5
AMBIENT_WAIT_POOL_CAPACITY = 1
AMBIENT_WAIT_MEMORY_TARGET = ":memory:"
AMBIENT_WAIT_PROBE_SQL = "SELECT 1"


def assert_ambient_admission_owner_retained(ownership, attempt, *, explicit_budget=None):
    occupied = threading.Event()
    release = threading.Event()

    def retain_ambient_admission_owner():
        with ownership():
            occupied.set()
            release.wait(timeout=AMBIENT_WAIT_SCOPE_S * AMBIENT_WAIT_HOLD_FACTOR)

    holder = threading.Thread(target=retain_ambient_admission_owner)
    holder.start()
    try:
        assert occupied.wait(timeout=config.LEDGER_WRITE_TIMEOUT_S)
        started = time.monotonic()
        with ledger_read.ledger_request_scope(deadline_s=AMBIENT_WAIT_SCOPE_S):
            attempt()
        allowed = AMBIENT_WAIT_SCOPE_S if explicit_budget is None else explicit_budget
        assert time.monotonic() - started < allowed * AMBIENT_WAIT_SCHEDULING_FACTOR
        assert holder.is_alive(), "admission must stop before conflicting owner releases"
    finally:
        release.set()
        holder.join(timeout=config.LEDGER_WRITE_TIMEOUT_S)
    assert not holder.is_alive()


def test_ambient_generation_admission_wait_is_bounded():
    def refuse_ambient_generation_admission():
        with pytest.raises(ledger_read.LedgerQueryDeadline):
            config.open_ledger_connection(AMBIENT_WAIT_MEMORY_TARGET, tune=False)

    assert_ambient_admission_owner_retained(lambda: config._LEDGER_ADMISSION.lock,
                                          refuse_ambient_generation_admission)


def test_ambient_legacy_pool_capacity_wait_is_bounded():
    def refuse_ambient_legacy_pool_admission():
        with pytest.raises(ledger_read.LedgerQueryDeadline):
            config.pooled_ledger_conn(__name__, lambda connection: None)

    def retain_ambient_managed_pool_capacity():
        return config.managed_ledger_read_connection(AMBIENT_WAIT_MEMORY_TARGET)

    with patch.object(config, "ledger_read_pool_settings", return_value=(
            AMBIENT_WAIT_POOL_CAPACITY, AMBIENT_WAIT_SCOPE_S * AMBIENT_WAIT_HOLD_FACTOR)):
        assert_ambient_admission_owner_retained(retain_ambient_managed_pool_capacity,
                                              refuse_ambient_legacy_pool_admission)


def test_ambient_schema_lock_wait_is_bounded():
    with contextlib.closing(config.open_ledger_connection(AMBIENT_WAIT_MEMORY_TARGET, tune=False)) as connection:
        def refuse_ambient_schema_admission():
            with pytest.raises(ledger_read.LedgerQueryDeadline):
                config.ensure_ledger_schema(connection, lambda schema_connection: None)

        assert_ambient_admission_owner_retained(lambda: config._LEDGER_SCHEMA_LOCK,
                                              refuse_ambient_schema_admission)


def test_ambient_cursor_step_lock_wait_is_bounded():
    with contextlib.closing(config.open_ledger_connection(AMBIENT_WAIT_MEMORY_TARGET, tune=False)) as connection:
        cursor = connection.cursor()
        try:
            def refuse_ambient_cursor_step():
                with pytest.raises(ledger_read.LedgerQueryDeadline):
                    cursor.execute(AMBIENT_WAIT_PROBE_SQL)

            assert_ambient_admission_owner_retained(lambda: connection._ledger_request_step_lock,
                                                  refuse_ambient_cursor_step)
        finally:
            cursor.close()


def test_ambient_wal_generation_wait_uses_earlier_expiry():
    def refuse_ambient_wal_generation():
        with pytest.raises(ledger_read.LedgerQueryDeadline):
            ledger_wal_maintenance.truncate_ledger_wal_bounded(
                deadline_s=AMBIENT_WAIT_SCOPE_S * AMBIENT_WAIT_HOLD_FACTOR)

    assert_ambient_admission_owner_retained(lambda: config._LEDGER_ADMISSION.lock,
                                          refuse_ambient_wal_generation)


def test_ambient_generation_shorter_explicit_expiry_wins():
    shorter_budget = AMBIENT_WAIT_SCOPE_S / AMBIENT_WAIT_SCHEDULING_FACTOR

    def refuse_shorter_generation_admission():
        with pytest.raises(ledger_read.LedgerQueryDeadline):
            config.open_ledger_connection(AMBIENT_WAIT_MEMORY_TARGET, tune=False,
                                         deadline_at=time.monotonic() + shorter_budget)

    assert_ambient_admission_owner_retained(lambda: config._LEDGER_ADMISSION.lock,
                                          refuse_shorter_generation_admission,
                                          explicit_budget=shorter_budget)


def test_ambient_wal_shorter_explicit_expiry_wins():
    shorter_wal_budget = AMBIENT_WAIT_SCOPE_S / AMBIENT_WAIT_SCHEDULING_FACTOR

    def refuse_shorter_wal_admission():
        with pytest.raises(ledger_read.LedgerQueryDeadline):
            ledger_wal_maintenance.truncate_ledger_wal_bounded(deadline_s=shorter_wal_budget)

    assert_ambient_admission_owner_retained(lambda: config._LEDGER_ADMISSION.lock,
                                          refuse_shorter_wal_admission,
                                          explicit_budget=shorter_wal_budget)


if __name__ == "__main__":
    sys.exit(run_pytest_style(globals()))
