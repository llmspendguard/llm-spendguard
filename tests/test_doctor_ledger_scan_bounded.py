from unittest import mock

from spendguard.analyze.reconciliation import cost_integrity
from spendguard.store.spend_search.ledger_read_contracts import LedgerReadTimeout

LEDGER_SCAN_TEST_BUDGET_SECONDS = 0.25


def guard_cost_scan_reports_declared_bound():
    declared_budget = LEDGER_SCAN_TEST_BUDGET_SECONDS
    with mock.patch.object(cost_integrity.ledger_read_accessors, "read_metered_cost_buckets",
                           side_effect=LedgerReadTimeout(declared_budget)):
        result = cost_integrity.audit_metered_cost_vs_tokens()
        lines = cost_integrity.cost_integrity_lines()
    assert result["ok"] is False
    assert str(declared_budget) in result["error"]
    assert any(str(declared_budget) in line and "UNKNOWN" in line for line in lines)


if __name__ == "__main__":
    guard_cost_scan_reports_declared_bound()
    print("GREEN: bounded ledger timeout returns promptly with a named health gap")
