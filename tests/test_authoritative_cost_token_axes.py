"""Offline regressions: authoritative dollars, token observations, and contracts are independent."""
import contextlib
import io
import os
import sys
from types import SimpleNamespace
from unittest.mock import patch

from _pytest_fixture_bridge import align_test_startup_home

align_test_startup_home(prefix=__name__)
os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"
os.environ["SPENDGUARD_CALLS"] = "1"

from spendguard import adapters, budget, calls, focus_export, gate, lane_value, litellm_adapter, output_contract, pricing, receipt

AXES_MODEL = "gpt-5-nano"
AXES_INPUT = 100
AXES_INTENT = "authoritative-cost-token-axes"
AXES_MALFORMED_OUTPUT = "malformed structured response"
AXES_OUTPUT_CONTRACT = "json"
AXES_ROLLUP_INTENT = AXES_INTENT + "-money-rollup"


def test_authoritative_unknown_usage_survives_money_and_realtime_telemetry():
    cost = pricing.realtime_cost(AXES_MODEL, AXES_INPUT, AXES_INPUT)
    with patch.object(budget, "_ledger") as ledger_factory:
        budget._record_spend_event("openai", AXES_MODEL, "realtime", cost,
                                   in_tok=AXES_INPUT, out_tok=None)
        event = ledger_factory.return_value.record_event.call_args.args[0]
    assert event["out_tok"] is None, event
    assert focus_export.focus_row(event)["PricingQuantity"] is None
    with patch.object(gate, "_rt_agg", {}), patch.object(gate, "_budget_record") as money_record, patch.object(gate, "_emit"):
        gate._rt_record("openai", AXES_MODEL, cost, in_tok=AXES_INPUT, out_tok=None)
        assert money_record.call_args.kwargs["out_tok"] is None
        gate._rt_record("openai", AXES_MODEL, cost, in_tok=AXES_INPUT, out_tok=AXES_INPUT)
        telemetry = next(iter(gate._rt_agg.values()))
        assert telemetry[3] is None, telemetry
        assert telemetry[1] == cost + cost
    with patch.object(budget, "_ledger") as ledger_factory:
        ledger = ledger_factory.return_value
        ledger._conn.execute.return_value.fetchall.return_value = [(AXES_INTENT,)]
        ledger.get.return_value = {"provider": "openai", "model": AXES_MODEL,
                                   "in_tok": AXES_INPUT, "out_tok": None}
        with patch.object(pricing, "realtime_cost") as reprice:
            plan = budget.reprice_unpriced()
        assert plan[0]["out_tok"] is None and plan[0]["cost"] is None
        assert plan[0]["method"] == "skip" and "unknown token usage" in plan[0]["reason"]
        reprice.assert_not_called()


def test_money_rollup_propagates_unknown_tokens_without_changing_dollars():
    cost = pricing.realtime_cost(AXES_MODEL, AXES_INPUT, AXES_INPUT)
    for output in (AXES_INPUT, None):
        budget._record_spend_event("openai", AXES_MODEL, "realtime", cost,
                                   in_tok=AXES_INPUT, out_tok=output, intent=AXES_ROLLUP_INTENT)
    grouped = budget._ledger().sum_by("intent", where={"intent": AXES_ROLLUP_INTENT},
                                     int_cols=["in_tok", "out_tok"])[AXES_ROLLUP_INTENT]
    assert grouped["out_tok"] is None, grouped
    assert grouped["in_tok"] == AXES_INPUT + AXES_INPUT
    assert float(grouped["usd"]) == cost + cost


def test_authoritative_cost_unknown_usage_reaches_every_receipt_axis():
    executor = next(iter(name for name, _module in adapters._LANES.values()))
    cost = pricing.realtime_cost(AXES_MODEL, AXES_INPUT, AXES_INPUT)
    start = calls._max_rowid()
    calls.record_call("openai", AXES_MODEL, "subscription", cost, in_tok=AXES_INPUT,
                      out_tok=None, intent=AXES_INTENT, executor=executor)
    assert_axes_latest(None, cost, 0)
    aggregate = calls.flow_agg(start)
    assert aggregate["out_tok"] is None, aggregate
    assert aggregate["cost"] == cost
    assert lane_value.stamp_from_ledger()[executor] is None
    flow = dict(aggregate, actual=aggregate["cost"], intent=AXES_INTENT)
    line = receipt.render_flow(flow, "flow", {})
    assert "out unknown" in line, line
    assert "out 0" not in line, line
    assert f"actual {receipt._money(cost)}" in line, line
    with patch.object(receipt, "level", return_value="flow"), patch.object(
        receipt, "tally", return_value={}
    ), patch.object(receipt, "_out") as emitted:
        receipt.emit_flow(AXES_INTENT, None, (start, None))
    assert "out unknown" in emitted.call_args.args[0]


def assert_axes_latest(expected_output, expected_cost, expected_tokens):
    assert calls._calls_db().execute(
        "SELECT out_tok,cost,settlement_complete,token_usage_complete FROM calls "
        "ORDER BY rowid DESC LIMIT 1").fetchone() == (expected_output, expected_cost, 1, expected_tokens)
    with patch.object(gate, "_allow", return_value=False):
        gate._rt_precheck_guard(lambda kw: (AXES_MODEL, AXES_INPUT, 0), {})


def test_litellm_embedding_zero_keeps_metered_admission():
    cost = pricing.realtime_cost(AXES_MODEL, AXES_INPUT, 0)
    with calls.context(intent=AXES_INTENT):
        for operation in ("embedding", "aembedding", "rerank", "arerank", SimpleNamespace(value="embedding")):
            litellm_adapter.record_litellm(
                {"model": AXES_MODEL, "call_type": operation, "response_cost": cost},
                {"usage": {"prompt_tokens": AXES_INPUT, "total_tokens": AXES_INPUT}})
            assert_axes_latest(0, cost, 1)
        litellm_adapter.record_litellm(
            {"model": AXES_MODEL, "response_cost": cost},
            {"object": "list", "usage": {"prompt_tokens": AXES_INPUT}})
        assert_axes_latest(0, cost, 1)


def test_authoritative_cost_survives_every_shared_writer():
    cost = pricing.realtime_cost(AXES_MODEL, AXES_INPUT, AXES_INPUT)
    for kind in ("realtime", "batch", "subscription"):
        for writer in (calls.record_call, calls.insert):
            writer("openai", AXES_MODEL, kind, cost, in_tok=AXES_INPUT,
                   out_tok=None, intent=AXES_INTENT)
            assert_axes_latest(None, cost, 0)
    assert calls.reconcile_calls_batch_estimate(
        "openai", AXES_MODEL, AXES_INTENT, AXES_INPUT, None, cost)
    assert_axes_latest(None, cost, 0)
    with calls.context(intent=AXES_INTENT):
        gate._record_rt(AXES_MODEL, {}, AXES_INPUT, None, cost=cost)
        assert_axes_latest(None, cost, 0)
        litellm_adapter.record_litellm(
            {"model": AXES_MODEL, "call_type": "completion", "response_cost": cost},
            {"usage": {"prompt_tokens": AXES_INPUT}})
        assert_axes_latest(None, cost, 0)
        for authoritative_cost in (cost, 0):
            litellm_adapter.record_litellm(
                {"model": AXES_MODEL, "call_type": "completion", "response_cost": authoritative_cost}, {})
            assert_axes_latest(None, authoritative_cost, 0)


def test_partial_usage_still_validates_output_contract():
    stderr = io.StringIO()
    with calls.context(intent=AXES_INTENT, contract=AXES_OUTPUT_CONTRACT), patch.object(
        output_contract, "check_item", wraps=output_contract.check_item
    ) as check, contextlib.redirect_stderr(stderr):
        tally = calls._local.contract_tally
        for authoritative_cost in (None, pricing.realtime_cost(AXES_MODEL, AXES_INPUT, AXES_INPUT)):
            gate._record_rt(AXES_MODEL, {}, AXES_INPUT, None, output=AXES_MALFORMED_OUTPUT,
                            cost=authoritative_cost)
    assert check.call_count == 2
    assert tally["n"] == tally["failed"] == check.call_count
    assert "CONTRACT FAILED" in stderr.getvalue()


if __name__ == "__main__":
    axes_cases = {"embedding": test_litellm_embedding_zero_keeps_metered_admission,
                  "unknown": test_authoritative_cost_unknown_usage_reaches_every_receipt_axis,
                  "telemetry": test_authoritative_unknown_usage_survives_money_and_realtime_telemetry,
                  "money_rollup": test_money_rollup_propagates_unknown_tokens_without_changing_dollars,
                  "cost": test_authoritative_cost_survives_every_shared_writer,
                  "contract": test_partial_usage_still_validates_output_contract}
    for axes_case in sys.argv[1:] or axes_cases:
        axes_cases[axes_case]()
    print("authoritative cost token axes passed")
