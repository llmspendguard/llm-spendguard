"""Read deadlines expire while a callback still owns writer admission."""
import sys
import threading
import time

from _pytest_fixture_bridge import align_test_startup_home, run_pytest_style

align_test_startup_home(prefix=__name__)

from spendguard import budget, config, ledger, ledger_read, ledger_writer

WRITER_REPRO_DEADLINE_S = 0.02
WRITER_REPRO_HOLD_FACTOR = 20
WRITER_REPRO_SCHEDULING_FACTOR = 5
WRITER_REPRO_JOB = __name__
WRITER_SETUP_REPRO_POOL_KEY = __name__


def test_freshness_deadline_expires_before_writer_release():
    current = ledger.SpendLedger()
    occupied = threading.Event()
    release = threading.Event()
    hold_s = WRITER_REPRO_DEADLINE_S * WRITER_REPRO_HOLD_FACTOR

    def hold_freshness_writer_callback():
        occupied.set()
        release.wait(timeout=hold_s)

    holder = threading.Thread(target=lambda: ledger_writer.run_ledger_write(
        hold_freshness_writer_callback, current.db_path))
    holder.start()
    try:
        assert occupied.wait(timeout=config.LEDGER_WRITE_TIMEOUT_S)
        started = time.monotonic()
        try:
            current.running_llm_total_dec(deadline_s=WRITER_REPRO_DEADLINE_S)
        except ledger_read.LedgerQueryDeadline:
            elapsed = time.monotonic() - started
        else:
            raise AssertionError("writer admission must refuse the expired aggregate")
        print(f"writer deadline elapsed={elapsed:.6f}s hold={hold_s:.6f}s")
        assert elapsed < WRITER_REPRO_DEADLINE_S * WRITER_REPRO_SCHEDULING_FACTOR
        assert holder.is_alive(), "deadline must precede writer release"
    finally:
        release.set()
        holder.join(timeout=config.LEDGER_WRITE_TIMEOUT_S)
    assert not holder.is_alive()


def assert_setup_read_refuses_occupied_writer(read_callback):
    ledger.SpendLedger()
    budget._reset_ledger()
    setup_occupied = threading.Event()
    setup_release = threading.Event()
    setup_hold_s = WRITER_REPRO_DEADLINE_S * WRITER_REPRO_HOLD_FACTOR

    def hold_setup_writer_callback():
        setup_occupied.set()
        setup_release.wait(timeout=setup_hold_s)

    setup_holder = threading.Thread(target=lambda: ledger_writer.run_ledger_write(
        hold_setup_writer_callback))
    setup_holder.start()
    try:
        assert setup_occupied.wait(timeout=config.LEDGER_WRITE_TIMEOUT_S)
        setup_started = time.monotonic()
        try:
            read_callback()
        except ledger_read.LedgerQueryDeadline:
            setup_elapsed = time.monotonic() - setup_started
        else:
            raise AssertionError("setup admission must refuse the expired read")
        assert setup_elapsed < WRITER_REPRO_DEADLINE_S * WRITER_REPRO_SCHEDULING_FACTOR
        assert setup_holder.is_alive(), "setup deadline must precede writer release"
    finally:
        setup_release.set()
        setup_holder.join(timeout=config.LEDGER_WRITE_TIMEOUT_S)
    assert not setup_holder.is_alive()


def test_budget_first_touch_admission_is_inside_deadline():
    for setup_accessor in (
            lambda: budget.spent_all_time(deadline_s=WRITER_REPRO_DEADLINE_S),
            lambda: budget.spent_since(None, deadline_s=WRITER_REPRO_DEADLINE_S),
            lambda: budget.spent_by_job(WRITER_REPRO_JOB, deadline_s=WRITER_REPRO_DEADLINE_S)):
        assert_setup_read_refuses_occupied_writer(setup_accessor)


def test_request_scope_schema_admission_is_inside_deadline():
    def read_with_request_schema_setup():
        with ledger_read.ledger_request_scope(deadline_s=WRITER_REPRO_DEADLINE_S):
            with config.ledger_op(WRITER_SETUP_REPRO_POOL_KEY, lambda connection: None):
                raise AssertionError("schema admission must expire before yielding")

    assert_setup_read_refuses_occupied_writer(read_with_request_schema_setup)


if __name__ == "__main__":
    sys.exit(run_pytest_style(globals()))
