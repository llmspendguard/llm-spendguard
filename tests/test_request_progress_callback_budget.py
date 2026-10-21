"""Count complete SQLite callback work and fault-test request deadlines offline."""
import contextlib
import os
from pathlib import Path
import sys
import tempfile
import threading
import time

os.environ.setdefault("SPENDGUARD_TEST_ISOLATED", "1")
os.environ.setdefault("SPENDGUARD_HOME", tempfile.mkdtemp())

import spendguard
from spendguard import config, ledger_read

PROGRESS_GUARD_DATABASE = "request-progress.sqlite"
PROGRESS_GUARD_COMPLETE_STEPS = 100000
PROGRESS_GUARD_ADVERSE_STEPS = 100000000
# A full recursive aggregate executes approximately eighteen opcodes per row.
# Leave SQLite-version headroom while rejecting the old hundred-opcode polling.
PROGRESS_GUARD_CALLBACK_CEILING = 2000
PROGRESS_GUARD_DEADLINE_S = 0.02
PROGRESS_GUARD_COMPLETION_BUDGET_S = 5.0
PROGRESS_GUARD_DEADLINE_HEADROOM_S = 0.5
PROGRESS_GUARD_CANCEL_WAIT_S = 1.0
PROGRESS_GUARD_CANCEL_THREAD = "request-progress-cancellation"
PROGRESS_GUARD_PROFILE_EVENT = "call"
PROGRESS_GUARD_CALLBACK_NAMES = frozenset((
    "enforce_request_cursor_progress", "ledger_read_progress"))
PROGRESS_GUARD_SQL = """WITH RECURSIVE counter(value) AS
    (SELECT 1 UNION ALL SELECT value + 1 FROM counter WHERE value < ?)
    SELECT SUM(value) FROM counter"""


def run_request_progress_callback_budget_guards():
    spendguard.require()
    with tempfile.TemporaryDirectory() as progress_directory:
        progress_path = Path(progress_directory) / PROGRESS_GUARD_DATABASE
        with contextlib.closing(config.open_ledger_connection(progress_path)) as connection:
            callback_counts = {}

            def observe_request_progress_callback(frame, event, argument):
                if event == PROGRESS_GUARD_PROFILE_EVENT and frame.f_code.co_name in PROGRESS_GUARD_CALLBACK_NAMES:
                    callback_name = frame.f_code.co_name
                    callback_counts[callback_name] = callback_counts.get(callback_name, 0) + 1

            previous_profiler = sys.getprofile()
            sys.setprofile(observe_request_progress_callback)
            try:
                managed_total = connection.execute(
                    PROGRESS_GUARD_SQL, (PROGRESS_GUARD_COMPLETE_STEPS,)).fetchone()[0]
                bounded_total = ledger_read.read_ledger_sql(ledger_read.LedgerSQLReadRequest(
                    PROGRESS_GUARD_SQL, (PROGRESS_GUARD_COMPLETE_STEPS,),
                    time_budget=PROGRESS_GUARD_COMPLETION_BUDGET_S), path=progress_path)[0][0]
            finally:
                sys.setprofile(previous_profiler)
            assert managed_total == bounded_total == sum(range(PROGRESS_GUARD_COMPLETE_STEPS + 1))
            for callback_name in PROGRESS_GUARD_CALLBACK_NAMES:
                callback_count = callback_counts.get(callback_name, 0)
                print(f"{callback_name}: {callback_count} callbacks")
                assert 0 < callback_count < PROGRESS_GUARD_CALLBACK_CEILING, callback_counts

            for bounded_owner in (False, True):
                started = time.monotonic()
                try:
                    with ledger_read.ledger_request_scope(deadline_s=PROGRESS_GUARD_DEADLINE_S):
                        if bounded_owner:
                            ledger_read.read_ledger_sql(ledger_read.LedgerSQLReadRequest(
                                PROGRESS_GUARD_SQL, (PROGRESS_GUARD_ADVERSE_STEPS,),
                                time_budget=PROGRESS_GUARD_DEADLINE_S), path=progress_path)
                        else:
                            connection.execute(PROGRESS_GUARD_SQL, (PROGRESS_GUARD_ADVERSE_STEPS,)).fetchone()
                except ledger_read.LedgerReadTimeout:
                    pass
                else:
                    raise AssertionError("adverse scan must raise a typed deadline error")
                elapsed = time.monotonic() - started
                assert elapsed <= PROGRESS_GUARD_DEADLINE_S + PROGRESS_GUARD_DEADLINE_HEADROOM_S, elapsed
                print(f"deadline bounded_owner={bounded_owner}: {elapsed:.6f}s")

            with ledger_read.ledger_request_scope(deadline_s=PROGRESS_GUARD_COMPLETION_BUDGET_S) as scope:
                cancellation_observed = threading.Event()
                previous_register = scope.register_request_read_connection

                def register_progress_guard_cancellation(registered_connection):
                    previous_register(registered_connection)
                    cancellation_observed.set()

                scope.register_request_read_connection = register_progress_guard_cancellation

                def cancel_progress_guard_inflight_read():
                    if cancellation_observed.wait(PROGRESS_GUARD_CANCEL_WAIT_S):
                        scope.cancel_ledger_request()

                canceller = threading.Thread(target=cancel_progress_guard_inflight_read,
                                             name=PROGRESS_GUARD_CANCEL_THREAD)
                canceller.start()
                try:
                    connection.execute(PROGRESS_GUARD_SQL, (PROGRESS_GUARD_ADVERSE_STEPS,)).fetchone()
                except ledger_read.LedgerRequestAbandoned:
                    print("in-flight transport cancellation: typed refusal")
                else:
                    raise AssertionError("cancelled scan must refuse with a typed abandonment")
                finally:
                    canceller.join(PROGRESS_GUARD_CANCEL_WAIT_S)
                assert cancellation_observed.is_set() and not canceller.is_alive()
    print("request progress callback budget: PASS")


if __name__ == "__main__":
    run_request_progress_callback_budget_guards()
