"""Script guards for migrated best-effort ledger read error contracts."""
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

from spendguard import calibrate, config, lane_balance, ledger_read, receipt

DEGRADE_TEST_DATABASE = "absent-ledger.sqlite"
DEGRADE_TEST_INTENT = "contract-guard"
DEGRADE_TEST_ERROR = "injected unreadable ledger"


def check_ledger_degrade_contract(label, operation, expected, failures):
    try:
        actual = operation()
        passed = actual == expected
    except Exception as error:
        passed = False
        print(f"  {type(error).__name__}: {error}")
    print(f"[{'OK' if passed else 'FAIL'}] {label}")
    if not passed:
        failures.append(label)


def run_ledger_degrade_contract_guards():
    failures = []
    with tempfile.TemporaryDirectory() as directory:
        missing_path = Path(directory) / DEGRADE_TEST_DATABASE
        with patch.object(config, "db_path", return_value=missing_path):
            check_ledger_degrade_contract(
                "absent ledger returns empty recent counts",
                lane_balance._recent_calls_by_lane, {}, failures)
            with patch.object(receipt, "_plan_usd", return_value=(0.0, False)):
                check_ledger_degrade_contract(
                    "lane utilization succeeds with absent ledger",
                    lambda: all(row["calls_recent"] == 0 for row in lane_balance.lane_utilization()["lanes"]),
                    True, failures)
        with patch.object(ledger_read, "read_lane_recent_counts",
                          side_effect=ledger_read.LedgerReadError(DEGRADE_TEST_ERROR)):
            check_ledger_degrade_contract(
                "unreadable ledger returns empty recent counts",
                lane_balance._recent_calls_by_lane, {}, failures)
        with patch.object(ledger_read, "read_known_call_intents",
                          side_effect=ledger_read.LedgerReadError(DEGRADE_TEST_ERROR)):
            with patch.object(config, "_cfg_get", return_value=[DEGRADE_TEST_INTENT]):
                check_ledger_degrade_contract(
                    "coverage read failure remains unknown rather than dead",
                    lambda: all(not row["read_ok"] and row["unmatched"] is None
                                for row in lane_balance.bandit_list_coverage().values()),
                    True, failures)
        with patch.object(config, "db_path", side_effect=RuntimeError(DEGRADE_TEST_ERROR)):
            try:
                lane_balance.bandit_list_coverage()
            except RuntimeError:
                print("[OK] coverage preserves deliberate config stop")
            else:
                failures.append("coverage preserves deliberate config stop")
                print("[FAIL] coverage preserves deliberate config stop")
        with patch.object(ledger_read, "read_calibration_calls",
                          side_effect=ledger_read.LedgerReadSchemaError(DEGRADE_TEST_ERROR)):
            check_ledger_degrade_contract(
                "query-only calibration handler degrades schema failure",
                lambda: calibrate._calls_rows(None, None, None), [], failures)
        for error in (ledger_read.LedgerReadOpenError(DEGRADE_TEST_ERROR),
                      ledger_read.LedgerReadTimeout(ledger_read.DEFAULT_LEDGER_READ_TIME_BUDGET_S),
                      ledger_read.LedgerReadTooLarge(ledger_read.DEFAULT_LEDGER_READ_ROW_CAP,
                                                   ledger_read.DEFAULT_LEDGER_READ_ROW_CAP)):
            label = f"query-only calibration handler propagates {type(error).__name__}"
            with patch.object(ledger_read, "read_calibration_calls", side_effect=error):
                try:
                    calibrate._calls_rows(None, None, None)
                except type(error):
                    print(f"[OK] {label}")
                else:
                    print(f"[FAIL] {label}")
                    failures.append(label)
        with patch.object(config, "db_path", return_value=missing_path):
            try:
                calibrate._calls_rows(None, None, None)
            except ledger_read.LedgerReadOpenError:
                print("[OK] actual missing ledger preserves query-only open failure")
            else:
                print("[FAIL] actual missing ledger preserves query-only open failure")
                failures.append("actual missing ledger preserves query-only open failure")
        with patch.object(config, "db_path", side_effect=RuntimeError(DEGRADE_TEST_ERROR)):
            try:
                calibrate._calls_rows(None, None, None)
            except RuntimeError:
                print("[OK] query-only calibration preserves config stop")
            else:
                print("[FAIL] query-only calibration preserves config stop")
                failures.append("query-only calibration preserves config stop")
    print(f"ledger degrade contracts: {len(failures)} failure(s)")
    return bool(failures)


if __name__ == "__main__":
    sys.exit(run_ledger_degrade_contract_guards())
