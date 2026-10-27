"""Offline token aggregates retain NULL, including mixed known/unknown calls."""
import os
import tempfile
from _pytest_fixture_bridge import align_test_startup_home

align_test_startup_home(prefix=__name__)
os.environ.setdefault("SPENDGUARD_HOME", tempfile.mkdtemp())
os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"

from spendguard import calls, ledger_read

TOKEN_AGGREGATE_EXECUTOR = "gemini"
TOKEN_AGGREGATE_MODEL = "aggregate-fixture"
TOKEN_AGGREGATE_INPUT = 12
TOKEN_AGGREGATE_OUTPUT = 7
TOKEN_AGGREGATE_COST = 1.0


def test_mixed_token_aggregate_preserves_unknown():
    calls._calls_db()
    baseline = ledger_read.read_call_max_rowid()
    for output in (TOKEN_AGGREGATE_OUTPUT, None):
        calls.insert("fixture", TOKEN_AGGREGATE_MODEL, "subscription", TOKEN_AGGREGATE_COST,
                     in_tok=TOKEN_AGGREGATE_INPUT, out_tok=output)
    calls._calls_db().execute("UPDATE calls SET executor=?", (TOKEN_AGGREGATE_EXECUTOR,))
    calls._calls_db().commit()
    flow = ledger_read.read_call_flow(baseline)
    assert flow["out_tok"] is None, flow
    assert flow["in_tok"] == TOKEN_AGGREGATE_INPUT * 2
    assert flow["cost"] == TOKEN_AGGREGATE_COST * 2
    metrics = ledger_read.read_lane_token_metrics(TOKEN_AGGREGATE_EXECUTOR)
    assert metrics["output"] is None and metrics["total"] is None, metrics
    assert ledger_read.read_lane_cumulative_tokens()[0]["tokens"] is None
    assert ledger_read.read_receipt_lane_activity("")[0]["out_tok"] is None


def test_explicit_incomplete_flag_overrides_numeric_output():
    # Legacy/externally imported rows may retain a number alongside an explicit gap.
    calls._calls_db().execute("UPDATE calls SET out_tok=?,token_usage_complete=0",
                              (TOKEN_AGGREGATE_OUTPUT,))
    calls._calls_db().commit()
    assert ledger_read.read_call_flow()["out_tok"] is None
    assert ledger_read.read_lane_subscription_calls("", {TOKEN_AGGREGATE_EXECUTOR})[0]["out_tok"] is None


if __name__ == "__main__":
    test_mixed_token_aggregate_preserves_unknown()
    test_explicit_incomplete_flag_overrides_numeric_output()
    print("token aggregates preserve unknown")
