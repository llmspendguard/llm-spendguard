"""Fault-inject offline ownership deadlines without weakening request readers."""
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import time

os.environ.setdefault("SPENDGUARD_TEST_ISOLATED", "1")
os.environ.setdefault("SPENDGUARD_HOME", tempfile.mkdtemp())

import spendguard
from spendguard import config, ledger_read
from spendguard import reconcile_maintenance_owner as maintenance
from spendguard.ledger_writer import current_ledger_writer_connection, run_ledger_write

MAINTENANCE_GUARD_DATABASE = "maintenance-deadline.sqlite"
MAINTENANCE_GUARD_BUDGET_S = 0.03
MAINTENANCE_GUARD_HEADROOM_S = 0.5
MAINTENANCE_GUARD_IDLE_MULTIPLIER = 2
MAINTENANCE_GUARD_INVALID_BUDGETS = (0, -1, float("nan"), float("inf"), True)
MAINTENANCE_GUARD_INVALID_ENV = "not-a-duration"
MAINTENANCE_GUARD_SQL = "SELECT 1"
MAINTENANCE_GUARD_LARGE_STEPS = 100000000
MAINTENANCE_GUARD_PROFILE_EVENT = "call"
MAINTENANCE_GUARD_PARSER_CALLBACK = "authorize_managed_statement_facts"
MAINTENANCE_GUARD_LIVE_CALLBACK = "require_reconcile_maintenance_live"
MAINTENANCE_GUARD_BOUNDARY_STEPS = 10000
# One execute and one fetch each validate ownership before and after SQLite.
MAINTENANCE_GUARD_EXPECTED_LIVE_BOUNDARIES = 4
MAINTENANCE_GUARD_LARGE_SQL = """WITH RECURSIVE counter(value) AS
    (SELECT 1 UNION ALL SELECT value + 1 FROM counter WHERE value < ?)
    SELECT SUM(value) FROM counter"""


def run_reconcile_maintenance_deadline_guards():
    spendguard.require()
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / MAINTENANCE_GUARD_DATABASE
        with maintenance.owned_reconcile_maintenance_connection(path) as owner:
            native_identity = id(owner._native_reconcile_connection)
            assert owner._native_reconcile_connection is current_ledger_writer_connection(path)
            assert owner._reconcile_budget == float(os.environ.get(
                maintenance.RECONCILE_MAINTENANCE_BUDGET_ENV,
                maintenance.RECONCILE_MAINTENANCE_DEFAULT_TIME_BUDGET_S))
            parser_callbacks = []
            live_callbacks = []

            def observe_reconcile_maintenance_parser_callback(frame, event, argument):
                if (event == MAINTENANCE_GUARD_PROFILE_EVENT
                        and frame.f_code.co_name == MAINTENANCE_GUARD_PARSER_CALLBACK):
                    parser_callbacks.append(frame.f_code.co_name)
                if (event == MAINTENANCE_GUARD_PROFILE_EVENT
                        and frame.f_code.co_name == MAINTENANCE_GUARD_LIVE_CALLBACK):
                    live_callbacks.append(frame.f_code.co_name)

            previous_profiler = sys.getprofile()
            sys.setprofile(observe_reconcile_maintenance_parser_callback)
            try:
                cursor = owner.cursor()
                assert not live_callbacks, live_callbacks
                cursor.execute(MAINTENANCE_GUARD_SQL)
                assert live_callbacks, "SQL execution must enforce the owner lifetime"
                live_callbacks.clear()
                aggregate_cursor = owner.execute(
                    MAINTENANCE_GUARD_LARGE_SQL, (MAINTENANCE_GUARD_BOUNDARY_STEPS,))
                assert aggregate_cursor.fetchone()[0] == (
                    MAINTENANCE_GUARD_BOUNDARY_STEPS * (MAINTENANCE_GUARD_BOUNDARY_STEPS + 1) // 2)
                assert len(live_callbacks) == MAINTENANCE_GUARD_EXPECTED_LIVE_BOUNDARIES, live_callbacks
            finally:
                sys.setprofile(previous_profiler)
            assert not parser_callbacks, parser_callbacks
            assert cursor.fetchone()[0] == 1
        try:
            cursor.fetchone()
        except sqlite3.ProgrammingError:
            pass
        else:
            raise AssertionError("retained offline cursor must close at owner exit")
        assert run_ledger_write(lambda: id(current_ledger_writer_connection(path)), path) == native_identity
        assert run_ledger_write(lambda: current_ledger_writer_connection(path).execute(
            MAINTENANCE_GUARD_SQL).fetchone()[0], path) == 1

        for invalid_budget in MAINTENANCE_GUARD_INVALID_BUDGETS:
            try:
                with maintenance.owned_reconcile_maintenance_connection(path, time_budget=invalid_budget):
                    raise AssertionError("invalid budget admitted offline ownership")
            except ValueError:
                pass
        previous_budget = os.environ.get(maintenance.RECONCILE_MAINTENANCE_BUDGET_ENV)
        os.environ[maintenance.RECONCILE_MAINTENANCE_BUDGET_ENV] = MAINTENANCE_GUARD_INVALID_ENV
        try:
            try:
                with maintenance.owned_reconcile_maintenance_connection(path):
                    raise AssertionError("invalid environment budget admitted offline ownership")
            except ValueError:
                pass
        finally:
            if previous_budget is None:
                os.environ.pop(maintenance.RECONCILE_MAINTENANCE_BUDGET_ENV)
            else:
                os.environ[maintenance.RECONCILE_MAINTENANCE_BUDGET_ENV] = previous_budget

        with ledger_read.ledger_request_scope(deadline_s=config.LEDGER_QUERY_DEADLINE_S):
            try:
                with maintenance.owned_reconcile_maintenance_connection(path):
                    raise AssertionError("request scope escaped managed reader containment")
            except maintenance.ReconcileMaintenanceRequestRefusal:
                pass

        started = time.monotonic()
        try:
            with maintenance.owned_reconcile_maintenance_connection(
                    path, time_budget=MAINTENANCE_GUARD_BUDGET_S) as owner:
                owner.execute(MAINTENANCE_GUARD_LARGE_SQL, (MAINTENANCE_GUARD_LARGE_STEPS,)).fetchone()
        except maintenance.ReconcileMaintenanceDeadline:
            pass
        else:
            raise AssertionError("large native scan must raise a typed deadline")
        assert time.monotonic() - started < MAINTENANCE_GUARD_BUDGET_S + MAINTENANCE_GUARD_HEADROOM_S

        try:
            with maintenance.owned_reconcile_maintenance_connection(
                    path, time_budget=MAINTENANCE_GUARD_BUDGET_S) as expired_owner:
                time.sleep(MAINTENANCE_GUARD_BUDGET_S * MAINTENANCE_GUARD_IDLE_MULTIPLIER)
                expired_cursor = expired_owner.cursor()
                try:
                    expired_cursor.execute(MAINTENANCE_GUARD_SQL)
                except maintenance.ReconcileMaintenanceDeadline:
                    pass
                else:
                    raise AssertionError("cursor allocated after expiry must refuse SQL")
        except maintenance.ReconcileMaintenanceDeadline:
            pass
        else:
            raise AssertionError("idle owner must refuse a silently complete result after deadline")

        with maintenance.owned_reconcile_maintenance_connection(
                path, time_budget=MAINTENANCE_GUARD_BUDGET_S) as owner:
            owner.commit_reconcile_maintenance_corrections()
            time.sleep(MAINTENANCE_GUARD_BUDGET_S * MAINTENANCE_GUARD_IDLE_MULTIPLIER)
            try:
                owner.execute(MAINTENANCE_GUARD_SQL)
            except sqlite3.ProgrammingError:
                pass
            else:
                raise AssertionError("final commit must prohibit further SQL without falsely refusing completed work")

        blocker = config.open_ledger_connection(path)
        try:
            blocker.execute("BEGIN IMMEDIATE")
            started = time.monotonic()
            try:
                with maintenance.owned_reconcile_maintenance_connection(
                        path, time_budget=MAINTENANCE_GUARD_BUDGET_S) as owner:
                    owner.execute("BEGIN IMMEDIATE")
            except maintenance.ReconcileMaintenanceDeadline:
                pass
            else:
                raise AssertionError("held writer lock must raise a typed deadline")
            assert time.monotonic() - started < MAINTENANCE_GUARD_BUDGET_S + MAINTENANCE_GUARD_HEADROOM_S
        finally:
            blocker.rollback()
            blocker.close()
    print("offline maintenance deadlines, lock fault, request refusal and cursor cleanup: PASS")


if __name__ == "__main__":
    run_reconcile_maintenance_deadline_guards()
