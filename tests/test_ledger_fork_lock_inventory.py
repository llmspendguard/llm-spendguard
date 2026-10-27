"""Discover process-owned ledger locks and prove fork replaces every one."""
import importlib
import multiprocessing
import os
import sys
import threading
import types

from _pytest_fixture_bridge import align_test_startup_home, run_pytest_style

align_test_startup_home(prefix=__name__)

LEDGER_FORK_INVENTORY_MODULES = (
    "config", "ledger_writer", "ledger_deadletter", "ledger_read", "ledger_request_cursor",
    "ledger_wal_maintenance", "mcp_fd_lifecycle", "budget", "calls", "callio", "learn",
    "semcache", "bulkgate", "batch_tracker", "content_retention",
)
LEDGER_FORK_INVENTORY_WATCHDOG_S = 10
LEDGER_FORK_LOCK_TYPES = (type(threading.Lock()), type(threading.RLock()), threading.Condition)


def discover_ledger_process_locks():
    inventory = {}
    for module_name in LEDGER_FORK_INVENTORY_MODULES:
        module = importlib.import_module("spendguard." + module_name)
        for state_name, state in tuple(vars(module).items()):
            if isinstance(state, LEDGER_FORK_LOCK_TYPES):
                inventory[(module.__name__, state_name, None)] = state
            elif (not isinstance(state, (type, types.ModuleType))
                  and type(state).__module__ == module.__name__ and hasattr(state, "__dict__")):
                for attribute, value in tuple(vars(state).items()):
                    if isinstance(value, LEDGER_FORK_LOCK_TYPES):
                        inventory[(module.__name__, state_name, attribute)] = value
    return inventory


def verify_child_ledger_lock_inventory(inherited):
    fresh = discover_ledger_process_locks()
    assert fresh.keys() == inherited.keys()
    for location, lock in fresh.items():
        assert lock is not inherited[location], location
        assert lock.acquire(blocking=False), location
        lock.release()


def test_all_discovered_ledger_process_locks_reset_after_fork():
    if not hasattr(os, "fork"):
        return
    inventory = discover_ledger_process_locks()
    assert inventory
    held = threading.Event()
    release = threading.Event()

    def hold_discovered_ledger_process_locks():
        acquired = []
        try:
            for lock in inventory.values():
                lock.acquire()
                acquired.append(lock)
            held.set()
            release.wait()
        finally:
            for lock in reversed(acquired):
                lock.release()

    owner = threading.Thread(target=hold_discovered_ledger_process_locks, daemon=True)
    child = multiprocessing.get_context("fork").Process(
        target=verify_child_ledger_lock_inventory, args=(inventory,))
    owner.start()
    try:
        assert held.wait(LEDGER_FORK_INVENTORY_WATCHDOG_S)
        child.start()
        child.join(LEDGER_FORK_INVENTORY_WATCHDOG_S)
        assert not child.is_alive(), "fork lock inventory child blocked"
        assert child.exitcode == 0
    finally:
        if child.pid is not None and child.is_alive():
            child.kill()
            child.join(LEDGER_FORK_INVENTORY_WATCHDOG_S)
        release.set()
        owner.join(LEDGER_FORK_INVENTORY_WATCHDOG_S)


if __name__ == "__main__":
    sys.exit(run_pytest_style(globals()))
