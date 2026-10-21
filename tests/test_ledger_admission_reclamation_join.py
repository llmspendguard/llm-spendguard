"""Concurrent admission must join ongoing garbage collection before refusing."""
import os
from pathlib import Path
import tempfile
import threading
import time

os.environ.setdefault("SPENDGUARD_TEST_ISOLATED", "1")
os.environ.setdefault("SPENDGUARD_HOME", tempfile.mkdtemp())

import spendguard
from spendguard import config

RECLAMATION_JOIN_CONNECTION_LIMIT = 2
RECLAMATION_JOIN_WAIT_S = 5.0
RECLAMATION_JOIN_OBSERVATION_S = 0.1
RECLAMATION_JOIN_REQUEST_BUDGET_S = 0.1
RECLAMATION_JOIN_DATABASE_NAME = "reclamation-join.sqlite"


def run_ledger_admission_reclamation_join_guard():
    spendguard.require()
    original_limit = config.ledger_connection_limit
    original_collect = config.gc.collect
    original_generation = config._LEDGER_ADMISSION
    config._LEDGER_ADMISSION = config._LedgerAdmissionGeneration()
    config.ledger_connection_limit = lambda: RECLAMATION_JOIN_CONNECTION_LIMIT
    collection_started = threading.Event()
    collection_release = threading.Event()
    competing_collection = threading.Event()
    second_admission_started = threading.Event()
    admitted = []
    failures = []
    workers = []
    expired = []
    try:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / RECLAMATION_JOIN_DATABASE_NAME
            for _ in range(RECLAMATION_JOIN_CONNECTION_LIMIT):
                expired.append(config.open_ledger_connection(database, tune=False))

            def simulate_nonreentrant_stalled_collection():
                if collection_started.is_set():
                    competing_collection.set()
                    return 0  # CPython returns immediately when collection is active.
                collection_started.set()
                # GC callbacks can reenter admission on the collector's thread.
                reclamation_lock = config._LEDGER_ADMISSION.reclamation_lock
                reentered = reclamation_lock.acquire(blocking=False)
                assert reentered, "collector callback would deadlock on admission"
                reclamation_lock.release()
                assert collection_release.wait(RECLAMATION_JOIN_WAIT_S)
                for connection in expired:
                    connection.close()
                return len(expired)

            config.gc.collect = simulate_nonreentrant_stalled_collection

            def attempt_reclamation_join_admission(second_caller=False):
                if second_caller:
                    second_admission_started.set()
                try:
                    admitted.append(config.open_ledger_connection(database, tune=False))
                except BaseException as error:
                    failures.append(error)

            first = threading.Thread(target=attempt_reclamation_join_admission)
            workers.append(first)
            first.start()
            assert collection_started.wait(RECLAMATION_JOIN_WAIT_S)
            # Request deadlines bypass GC rather than join an unbounded collector.
            started = time.monotonic()
            try:
                config.open_ledger_connection(
                    database, tune=False,
                    deadline_at=started + RECLAMATION_JOIN_REQUEST_BUDGET_S)
            except config.LedgerConnectionLimitError:
                pass
            else:
                raise AssertionError("request admission must refuse the occupied ceiling")
            assert time.monotonic() - started < RECLAMATION_JOIN_REQUEST_BUDGET_S
            second = threading.Thread(target=attempt_reclamation_join_admission, args=(True,))
            workers.append(second)
            second.start()
            assert second_admission_started.wait(RECLAMATION_JOIN_WAIT_S)
            overlapped = competing_collection.wait(RECLAMATION_JOIN_OBSERVATION_S)
            collection_release.set()
            for worker in workers:
                worker.join(RECLAMATION_JOIN_WAIT_S)
            assert not any(worker.is_alive() for worker in workers)
            assert not overlapped, "admission raced an unfinished collection"
            assert not failures, failures
            assert len(admitted) == RECLAMATION_JOIN_CONNECTION_LIMIT
            assert config.ledger_connection_count() == len(admitted)
    finally:
        collection_release.set()
        for worker in workers:
            worker.join(RECLAMATION_JOIN_WAIT_S)
        for connection in admitted + expired:
            connection.close()
        config.gc.collect = original_collect
        config.ledger_connection_limit = original_limit
        config._LEDGER_ADMISSION = original_generation
    print("ledger admission reclamation join: PASS")


if __name__ == "__main__":
    run_ledger_admission_reclamation_join_guard()
