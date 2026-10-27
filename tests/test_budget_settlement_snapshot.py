"""A committed input floor cannot slip between coverage and a complete total."""
import os
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from _pytest_fixture_bridge import align_test_startup_home

align_test_startup_home(prefix=__name__)
os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"

from spendguard import budget, calls, config, ledger_read

BUDGET_SNAPSHOT_FLOOR = 0.25
BUDGET_SNAPSHOT_DAY = budget._utc().strftime("%Y-%m-%d")
BUDGET_SNAPSHOT_SCOPES = ("read-only-snapshot", "conversation-snapshot")
BUDGET_SNAPSHOT_CALL_INSERT = """INSERT INTO calls
    (ts,project,settlement_complete,input_cost_floor) VALUES (?,?,0,?)"""
BUDGET_SNAPSHOT_EVENT_INSERT = """INSERT INTO spend_events
    (id,day,source,realtime_usd,project_primary,conv_id,status,is_meta,reconciled)
    VALUES (?,?,?,?,?,?,'posted',0,0)"""


def test_budget_coverage_and_spend_share_snapshot_during_incomplete_write():
    budget._ledger()
    calls._calls_db()
    snapshot_path = config.db_path()
    original_snapshot_coverage = ledger_read._read_incomplete_settlement_totals
    for snapshot_scope in BUDGET_SNAPSHOT_SCOPES:
        def commit_incomplete_snapshot_settlement(snapshot_scope=snapshot_scope):
            with sqlite3.connect(snapshot_path) as snapshot_writer:
                snapshot_writer.execute(BUDGET_SNAPSHOT_CALL_INSERT,
                                        (BUDGET_SNAPSHOT_DAY, snapshot_scope, BUDGET_SNAPSHOT_FLOOR))
                snapshot_writer.execute(BUDGET_SNAPSHOT_EVENT_INSERT,
                                        (snapshot_scope, BUDGET_SNAPSHOT_DAY, snapshot_scope,
                                         str(BUDGET_SNAPSHOT_FLOOR), snapshot_scope, snapshot_scope))

        def insert_floor_after_snapshot_coverage(*args, **kwargs):
            snapshot_coverage = original_snapshot_coverage(*args, **kwargs)
            assert snapshot_coverage["n"] == 0
            with ThreadPoolExecutor() as snapshot_pool:
                snapshot_pool.submit(commit_incomplete_snapshot_settlement).result()
            return snapshot_coverage

        snapshot_options = ({"read_only": True} if snapshot_scope == BUDGET_SNAPSHOT_SCOPES[0]
                            else {"conv": snapshot_scope})
        with patch.object(ledger_read, "_read_incomplete_settlement_totals",
                          side_effect=insert_floor_after_snapshot_coverage):
            snapshot_total = budget.spent_since(BUDGET_SNAPSHOT_DAY, project=snapshot_scope,
                                                **snapshot_options)
        assert snapshot_total == 0, f"input floor reported as complete: {snapshot_total}"
        assert ledger_read.read_incomplete_call_settlements(project=snapshot_scope)["n"] == 1
        try:
            budget.spent_since(BUDGET_SNAPSHOT_DAY, project=snapshot_scope, **snapshot_options)
        except ledger_read.LedgerSettlementIncomplete:
            pass
        else:
            raise AssertionError("subsequent snapshot must see committed incomplete settlement")


def test_meta_cap_compares_and_reports_one_measurement():
    with patch.object(config, "meta_cap", return_value=BUDGET_SNAPSHOT_FLOOR), patch.object(
        budget, "meta_spent_today", side_effect=(BUDGET_SNAPSHOT_FLOOR, 0)
    ) as snapshot_meta_read:
        assert budget.meta_exceeded(pending=BUDGET_SNAPSHOT_FLOOR) == (
            "meta", BUDGET_SNAPSHOT_FLOOR, BUDGET_SNAPSHOT_FLOOR + BUDGET_SNAPSHOT_FLOOR)
        snapshot_meta_read.assert_called_once_with()


if __name__ == "__main__":
    test_budget_coverage_and_spend_share_snapshot_during_incomplete_write()
    test_meta_cap_compares_and_reports_one_measurement()
    print("budget settlement snapshot guards passed")
