"""Periodic retry work cannot occupy the HTTP accept thread, offline."""
import sys
import threading
from unittest.mock import patch

from _pytest_fixture_bridge import align_test_startup_home, run_pytest_style

align_test_startup_home(prefix=__name__)

from spendguard import config, ledger_writer, mcp_fd_lifecycle as lifecycle
from spendguard import budget

ACCEPT_PROGRESS_BUDGET_S = 0.2
ACCEPT_WRITER_WATCHDOG_S = ACCEPT_PROGRESS_BUDGET_S * 20


def test_periodic_deadletter_service_returns_while_writer_held():
    for pending in (False, True):
        occupied, release, returned = threading.Event(), threading.Event(), threading.Event()
        retry_path = budget.spend_event_retry_paths()[0]
        if pending:
            retry_path.parent.mkdir(parents=True, exist_ok=True)
            retry_path.write_text("{}\n")
        elif retry_path.exists():
            retry_path.unlink()

        def hold_accept_progress_writer(occupied=occupied, release=release):
            occupied.set()
            release.wait(ACCEPT_WRITER_WATCHDOG_S)

        def dispatch_accept_progress_service(returned=returned):
            lifecycle.service_mcp_spend_deadletters()
            returned.set()

        holder = threading.Thread(target=lambda: ledger_writer.run_ledger_write(hold_accept_progress_writer))
        dispatcher = threading.Thread(target=dispatch_accept_progress_service)
        holder.start()
        try:
            assert occupied.wait(ACCEPT_WRITER_WATCHDOG_S)
            with patch.object(lifecycle, "_mcp_next_deadletter_drain_at", 0):
                dispatcher.start()
                assert returned.wait(ACCEPT_PROGRESS_BUDGET_S), "accept service waited for the ledger writer"
                assert holder.is_alive()
        finally:
            release.set()
            holder.join(ACCEPT_WRITER_WATCHDOG_S)
            dispatcher.join(ACCEPT_WRITER_WATCHDOG_S)
        assert not holder.is_alive() and not dispatcher.is_alive()
        if lifecycle._mcp_deadletter_worker is not None:
            lifecycle._mcp_deadletter_worker.join(ACCEPT_WRITER_WATCHDOG_S)
            assert not lifecycle._mcp_deadletter_worker.is_alive()


def test_deadletter_worker_deadline_retains_and_reschedules():
    retry_path = budget.spend_event_retry_paths()[0]
    retry_path.parent.mkdir(parents=True, exist_ok=True)
    evidence = "{}\n"
    retry_path.write_text(evidence)
    occupied, release = threading.Event(), threading.Event()

    def hold_retry_deadline_writer():
        occupied.set()
        release.wait(ACCEPT_WRITER_WATCHDOG_S)

    holder = threading.Thread(target=lambda: ledger_writer.run_ledger_write(hold_retry_deadline_writer))
    original_config_get = config._cfg_get

    def retry_deadline_test_policy(section, key, default=None):
        if key == "deadletter_drain_deadline_s":
            return ACCEPT_PROGRESS_BUDGET_S
        return original_config_get(section, key, default)

    holder.start()
    try:
        assert occupied.wait(ACCEPT_WRITER_WATCHDOG_S)
        with patch.object(config, "_cfg_get", side_effect=retry_deadline_test_policy), \
             patch.object(lifecycle, "_mcp_next_deadletter_drain_at", 0), \
             patch.object(lifecycle.MCP_FD_LOG, "exception") as warning:
            lifecycle.service_mcp_spend_deadletters()
            first_worker = lifecycle._mcp_deadletter_worker
            first_worker.join(ACCEPT_PROGRESS_BUDGET_S * 5)
            assert not first_worker.is_alive(), "retry admission exceeded its configured deadline"
            assert holder.is_alive() and retry_path.read_text() == evidence
            warning.assert_called_once()
            lifecycle._mcp_next_deadletter_drain_at = 0
            lifecycle.service_mcp_spend_deadletters()
            assert lifecycle._mcp_deadletter_worker is not first_worker
            lifecycle._mcp_deadletter_worker.join(ACCEPT_PROGRESS_BUDGET_S * 5)
            assert not lifecycle._mcp_deadletter_worker.is_alive()
            assert retry_path.read_text() == evidence
    finally:
        release.set()
        holder.join(ACCEPT_WRITER_WATCHDOG_S)


def test_empty_retry_spool_never_dispatches_a_writer():
    for retry_path in budget.spend_event_retry_paths():
        if retry_path.exists():
            retry_path.write_text("")
    with patch.object(lifecycle, "_mcp_next_deadletter_drain_at", 0), \
         patch("spendguard.ledger_deadletter.drain_spend_deadletter_events") as drain:
        lifecycle.service_mcp_spend_deadletters()
        drain.assert_not_called()


if __name__ == "__main__":
    sys.exit(run_pytest_style(globals()))
