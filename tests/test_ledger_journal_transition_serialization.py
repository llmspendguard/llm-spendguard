"""Concurrent cold ledger opens share one fully consumed WAL transition."""
import sys
import threading
import multiprocessing
import os
from unittest.mock import patch

from _pytest_fixture_bridge import align_test_startup_home, run_pytest_style

align_test_startup_home(prefix=__name__)

from spendguard import config

JOURNAL_TRANSITION_WATCHDOG_S = 10


def open_tuned_connection_in_fork_probe(path):
    config.open_ledger_connection(path).close()


def test_fork_child_tuned_open_replaces_held_journal_lock(tmp_path):
    if not hasattr(os, "fork"):
        return
    held = threading.Event()
    release = threading.Event()

    def hold_parent_journal_transition_lock():
        with config._LEDGER_JOURNAL_MODE_LOCK:
            held.set()
            release.wait()

    owner = threading.Thread(target=hold_parent_journal_transition_lock)
    child = multiprocessing.get_context("fork").Process(
        target=open_tuned_connection_in_fork_probe, args=(tmp_path / (__name__ + ".db"),))
    owner.start()
    try:
        assert held.wait(JOURNAL_TRANSITION_WATCHDOG_S)
        child.start()
        child.join(JOURNAL_TRANSITION_WATCHDOG_S)
        assert not child.is_alive(), "child tuned open blocked on inherited journal lock"
        assert child.exitcode == 0
    finally:
        if child.pid is not None and child.is_alive():
            child.kill()
            child.join(JOURNAL_TRANSITION_WATCHDOG_S)
        release.set()
        owner.join(JOURNAL_TRANSITION_WATCHDOG_S)


def test_concurrent_cold_opens_transition_journal_once(tmp_path):
    path = tmp_path / (__name__ + ".db")
    transition_entered = threading.Event()
    release_transition = threading.Event()
    second_started = threading.Event()
    statements = []
    failures = []
    execute = config._CountedLedgerConnection.execute

    def pause_first_journal_transition(connection, statement, *args, **kwargs):
        cursor = execute(connection, statement, *args, **kwargs)
        if statement == "PRAGMA journal_mode=WAL":
            statements.append(statement)
            if len(statements) == 1:
                transition_entered.set()
                assert release_transition.wait(JOURNAL_TRANSITION_WATCHDOG_S)
        return cursor

    def open_cold_journal_connection(started=None):
        try:
            if started is not None:
                started.set()
            config.open_ledger_connection(path).close()
        except BaseException as failure:
            failures.append(failure)

    with patch.object(config._CountedLedgerConnection, "execute", pause_first_journal_transition):
        first = threading.Thread(target=open_cold_journal_connection)
        second = threading.Thread(target=open_cold_journal_connection, args=(second_started,))
        first.start()
        try:
            assert transition_entered.wait(JOURNAL_TRANSITION_WATCHDOG_S)
            second.start()
            assert second_started.wait(JOURNAL_TRANSITION_WATCHDOG_S)
        finally:
            release_transition.set()
            first.join(JOURNAL_TRANSITION_WATCHDOG_S)
            if second.ident is not None:
                second.join(JOURNAL_TRANSITION_WATCHDOG_S)
    assert not first.is_alive() and not second.is_alive()
    assert not failures, failures
    assert len(statements) == 1, statements


if __name__ == "__main__":
    sys.exit(run_pytest_style(globals()))
