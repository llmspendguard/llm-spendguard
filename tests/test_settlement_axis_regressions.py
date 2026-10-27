"""Offline reproductions for monetary/token separation and partial meta attribution."""
import os
import sys
from unittest.mock import patch

from _pytest_fixture_bridge import align_test_startup_home

align_test_startup_home(prefix=__name__)
os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"

from spendguard import budget, calls, gate, ledger_read, pricing

AXIS_REGRESSION_MODEL = "gpt-5-nano"
AXIS_REGRESSION_INPUT = 100
AXIS_REGRESSION_PROJECT = "settlement-axis-regression"
AXIS_REGRESSION_INTENT = "settlement-axis-regression"
AXIS_REGRESSION_META_INTENT = "spendguard:settlement-axis-regression"


def test_subscription_token_gap_preserves_metered_admission():
    subscription_id = calls.record_call(
        "openai", AXIS_REGRESSION_MODEL, "subscription", 0,
        in_tok=AXIS_REGRESSION_INPUT, out_tok=None, intent=AXIS_REGRESSION_INTENT)
    with patch.object(gate, "_allow", return_value=False):
        gate._rt_precheck_guard(lambda kw: (AXIS_REGRESSION_MODEL, AXIS_REGRESSION_INPUT, 0), {})
    assert budget.spent_all_time() == 0
    assert ledger_read.read_incomplete_call_settlements()["n"] == 0
    assert calls._calls_db().execute(
        "SELECT cost,settlement_complete,token_usage_complete,out_tok FROM calls WHERE id=?",
        (subscription_id,)).fetchone() == (0, 1, 0, None)


def test_project_settlement_gap_normalizes_scope():
    calls.record_call("openai", AXIS_REGRESSION_MODEL, "realtime", None,
                      in_tok=AXIS_REGRESSION_INPUT, out_tok=None,
                      project=AXIS_REGRESSION_PROJECT, intent=AXIS_REGRESSION_INTENT)
    assert ledger_read.read_incomplete_call_settlements(
        project=f" {AXIS_REGRESSION_PROJECT.upper()} ")["n"] == 1
    try:
        budget.spent_since("", project=f" {AXIS_REGRESSION_PROJECT.upper()} ")
    except ledger_read.LedgerSettlementIncomplete:
        pass
    else:
        raise AssertionError("project-scoped total concealed incomplete monetary settlement")


def test_partial_meta_usage_uses_meta_accounting():
    expected_floor = pricing.input_cost_lower_bound(AXIS_REGRESSION_MODEL, AXIS_REGRESSION_INPUT)
    with calls.context(intent=AXIS_REGRESSION_META_INTENT), patch.object(
        budget, "record_meta", wraps=budget.record_meta
    ) as meta_booking, patch.object(gate, "_rt_record", wraps=gate._rt_record) as workload_booking:
        gate._record_rt(AXIS_REGRESSION_MODEL, {}, AXIS_REGRESSION_INPUT, None)
    meta_booking.assert_called_once_with("openai", AXIS_REGRESSION_MODEL, expected_floor)
    workload_booking.assert_not_called()
    assert calls._calls_db().execute(
        "SELECT cost,settlement_complete FROM calls ORDER BY rowid DESC LIMIT 1"
    ).fetchone() == (None, 0)


if __name__ == "__main__":
    selected_axis_tests = {
        "subscription": test_subscription_token_gap_preserves_metered_admission,
        "project": test_project_settlement_gap_normalizes_scope,
        "meta": test_partial_meta_usage_uses_meta_accounting,
    }
    for selected_axis_name in sys.argv[1:] or selected_axis_tests:
        selected_axis_tests[selected_axis_name]()
    print("settlement axis regressions passed")
