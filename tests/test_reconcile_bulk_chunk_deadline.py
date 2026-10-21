"""Prove offline chunks avoid VM callbacks and refuse adverse late completion."""
import contextlib
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import time

os.environ.setdefault("SPENDGUARD_TEST_ISOLATED", "1")
os.environ.setdefault("SPENDGUARD_HOME", tempfile.mkdtemp())

import spendguard
from spendguard import config, ledger_read, reconcile_estimate_stage

BULK_CHUNK_GUARD_DATABASE = "bulk-chunks.sqlite"
BULK_CHUNK_GUARD_EXTRA_ROWS = 1
BULK_CHUNK_GUARD_CHUNKS = 3
BULK_CHUNK_GUARD_BUDGET_S = 5.0
BULK_CHUNK_GUARD_TIGHT_BUDGET_S = 0.02
BULK_CHUNK_GUARD_ABANDON_DELAY_S = 0.03
BULK_CHUNK_GUARD_PROFILE_EVENT = "call"
BULK_CHUNK_GUARD_CALLBACK_NAME = "ledger_read_progress"
BULK_CHUNK_GUARD_SQL = """WITH RECURSIVE counter(value) AS
    (SELECT 1 UNION ALL SELECT value + 1 FROM counter WHERE value < ?)
    SELECT value FROM counter"""


def run_reconcile_bulk_chunk_deadline_guards():
    spendguard.require()
    row_count = (reconcile_estimate_stage.RECONCILE_BULK_FETCH_CHUNK_ROWS
                 * BULK_CHUNK_GUARD_CHUNKS + BULK_CHUNK_GUARD_EXTRA_ROWS)
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / BULK_CHUNK_GUARD_DATABASE
        with contextlib.closing(config.open_ledger_connection(path)):
            pass
        observed_counts = {}
        for bulk_mode in (False, True):
            callback_count = 0

            def observe_bulk_chunk_guard_callback(frame, event, argument):
                nonlocal callback_count
                if (event == BULK_CHUNK_GUARD_PROFILE_EVENT
                        and frame.f_code.co_name == BULK_CHUNK_GUARD_CALLBACK_NAME):
                    callback_count += 1

            previous_profiler = sys.getprofile()
            sys.setprofile(observe_bulk_chunk_guard_callback)
            try:
                with ledger_read._ledger_read_connection(
                        path, BULK_CHUNK_GUARD_BUDGET_S, bulk_chunked=bulk_mode) as connection:
                    with contextlib.closing(sqlite3.Connection.cursor(connection, sqlite3.Cursor)) as cursor:
                        cursor.execute(BULK_CHUNK_GUARD_SQL, (row_count,))
                        rows = (list(reconcile_estimate_stage._iter_reconcile_bulk_chunk_rows(cursor))
                                if bulk_mode else list(cursor))
                        assert [row[0] for row in rows] == list(range(1, row_count + 1))
            finally:
                sys.setprofile(previous_profiler)
            observed_counts[bulk_mode] = callback_count
        assert observed_counts[False] > 0, observed_counts
        assert observed_counts[True] == 0, observed_counts
        print(f"bulk callback count before={observed_counts[False]} after={observed_counts[True]}")

        try:
            with ledger_read._ledger_read_connection(
                    path, BULK_CHUNK_GUARD_TIGHT_BUDGET_S, bulk_chunked=True) as connection:
                with contextlib.closing(sqlite3.Connection.cursor(connection, sqlite3.Cursor)) as cursor:
                    cursor.execute(BULK_CHUNK_GUARD_SQL, (row_count,))
                    iterator = reconcile_estimate_stage._iter_reconcile_bulk_chunk_rows(cursor)
                    next(iterator)
                    time.sleep(BULK_CHUNK_GUARD_ABANDON_DELAY_S)
                    list(iterator)
        except ledger_read.LedgerReadTimeout:
            pass
        else:
            raise AssertionError("expired offline chunk returned a complete result")

        try:
            with ledger_read.ledger_request_scope(deadline_s=BULK_CHUNK_GUARD_BUDGET_S):
                with ledger_read._ledger_read_connection(
                        path, BULK_CHUNK_GUARD_BUDGET_S, bulk_chunked=True):
                    raise AssertionError("offline chunk owner entered a request scope")
        except ledger_read.LedgerReadError:
            pass
    print("reconcile bulk chunk deadline: PASS")


if __name__ == "__main__":
    run_reconcile_bulk_chunk_deadline_guards()
