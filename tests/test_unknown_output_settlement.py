"""An unreported completion must never become a complete free settlement (offline)."""
import os
from _pytest_fixture_bridge import align_test_startup_home

align_test_startup_home(prefix=__name__)
os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"

from spendguard import calls, pricing
from spendguard import advise, route_economics, lane_economics, lane_quota_cost, guard, calibrate, cost_integrity, cli
from unittest.mock import patch

UNKNOWN_SETTLEMENT_MODEL = "gpt-5-nano"
UNKNOWN_SETTLEMENT_INPUT = 100
UNKNOWN_SETTLEMENT_INTENT = "unknown-output-settlement-guard"


def test_advisor_unknown_tokens_never_produce_measured_value():
    calls.insert("openai", UNKNOWN_SETTLEMENT_MODEL, "subscription", 0.0,
                      in_tok=UNKNOWN_SETTLEMENT_INPUT, out_tok=None,
                      intent=UNKNOWN_SETTLEMENT_INTENT, quality="good", quality_conf=1.0)
    with patch.object(route_economics, "lane_eff_by_provider", return_value={"openai": 1.0}):
        evidence = advise.evidence(intent=UNKNOWN_SETTLEMENT_INTENT)
        assert evidence[f"openai:{UNKNOWN_SETTLEMENT_MODEL}"]["outtok"] is None
        ranked = advise.ranked(intent=UNKNOWN_SETTLEMENT_INTENT)
    arm = ranked["models"][0]
    assert arm["out_tok"] is None
    assert arm["per_m_out"] is None
    assert arm["valuation_complete"] is False
    assert arm["per_good"] is None
    assert ranked["pick"] is None


def test_lane_economics_unknown_token_metrics_stay_unknown():
    with patch("spendguard.ledger_read.read_lane_cumulative_tokens", return_value=[("test-lane", None)]):
        assert lane_economics._cum_tokens_by_executor()["test-lane"] is None
    with patch("spendguard.ledger_read.read_lane_token_metrics",
               return_value={"n": 1, "total": None, "input": 10, "output": None}):
        assert lane_economics._tokens_per_prompt("test-lane") is None
        assert lane_economics._used_split("test-lane") == (None, None)


def test_lane_quota_unknown_tokens_never_become_measured_average():
    from types import SimpleNamespace
    with patch("spendguard.ledger_read.read_ledger_sql",
               return_value=[SimpleNamespace(values=(UNKNOWN_SETTLEMENT_INTENT, 10, None))]):
        row = lane_quota_cost.measured_lane_token_cost()[UNKNOWN_SETTLEMENT_INTENT]
    assert row["tokens_per_call"] is None
    assert row["basis"] == "unknown"


def test_decision_unknown_counterfactual_propagates_through_rollups():
    guard.record_decision(UNKNOWN_SETTLEMENT_INTENT, UNKNOWN_SETTLEMENT_MODEL,
                          UNKNOWN_SETTLEMENT_MODEL, None, 0.0, None,
                          why="plan quota admission")
    assert guard.decisions_since()[0]["counterfactual_usd"] is None
    assert guard.decisions_summary()["saved_usd"] is None
    assert guard.decisions_summary(intent_limit=10)["saved_usd"] is None
    assert guard.decision_rollup()["by_route"][0]["counterfactual_usd"] is None
    assert guard.shed_summary()["quota_preserved_usd"] is None
    assert cli._dispatch(["savings"]) == 0


def test_calibration_unknown_actual_tokens_stay_unknown():
    import datetime
    from types import SimpleNamespace
    stamp = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
    connection = calibrate._con()
    connection.execute("INSERT INTO cost_predictions(job_id,ts,label,model) VALUES(?,?,?,?)",
                       (UNKNOWN_SETTLEMENT_INTENT, stamp, UNKNOWN_SETTLEMENT_INTENT, UNKNOWN_SETTLEMENT_MODEL))
    connection.commit()
    connection.close()
    with patch("spendguard.ledger_read.read_calibration_actuals", return_value=[
            SimpleNamespace(values=(UNKNOWN_SETTLEMENT_INPUT, None, 1.0, stamp, UNKNOWN_SETTLEMENT_MODEL))]):
        calibrate.pair()
    connection = calibrate._con()
    assert connection.execute("SELECT actual_out,actual_usd FROM cost_predictions WHERE job_id=?",
                              (UNKNOWN_SETTLEMENT_INTENT,)).fetchone() == (None, 1.0)
    connection.close()


def test_cost_integrity_unknown_tokens_have_no_manufactured_ceiling():
    assert cost_integrity._max_token_cost(UNKNOWN_SETTLEMENT_MODEL, UNKNOWN_SETTLEMENT_INPUT, None, False) is None


def test_measurement_producers_preserve_unknown_output_tokens():
    from spendguard import adapters, callio, effort_titration, bakeoff, vendor_call
    from types import SimpleNamespace
    response = {"provider": "openai", "model": UNKNOWN_SETTLEMENT_MODEL, "text": UNKNOWN_SETTLEMENT_INTENT,
                "in_tok": UNKNOWN_SETTLEMENT_INPUT, "out_tok": None, "cost": 0.0}
    with patch.object(adapters, "_call_guarded", return_value=response.copy()), patch.object(callio, "capture_live") as capture:
        adapters.call(UNKNOWN_SETTLEMENT_MODEL, UNKNOWN_SETTLEMENT_INTENT)
        assert capture.call_args.kwargs["out_tok"] is None
    with patch.object(adapters, "call", return_value=response.copy()), patch.object(effort_titration, "_score_output", return_value=None), patch.object(calls, "insert") as insertion:
        effort_titration._measure_one(UNKNOWN_SETTLEMENT_MODEL, None, UNKNOWN_SETTLEMENT_INTENT, 0,
                                     UNKNOWN_SETTLEMENT_INTENT, UNKNOWN_SETTLEMENT_MODEL, effort_titration._new_acc())
        assert insertion.call_args.kwargs["out_tok"] is None
    with patch.object(adapters, "call", return_value=response.copy()), patch.object(bakeoff, "_plan", return_value=(0.0, {}, 1, 1)), patch.object(bakeoff, "_judge_one", return_value=True), patch.object(calls, "finalize_bakeoff_measurement") as finalize:
        bakeoff.bakeoff(UNKNOWN_SETTLEMENT_INTENT, candidates=[UNKNOWN_SETTLEMENT_MODEL],
                        prompts=[UNKNOWN_SETTLEMENT_INTENT], run=True)
        assert finalize.call_args.args[6] is None
    admission = SimpleNamespace(ok=True, shed=False, release=lambda: None)
    with patch("spendguard.dispatch.admit", return_value=admission), patch.object(vendor_call, "_attempt", return_value=response.copy()):
        assert vendor_call.call("openai", UNKNOWN_SETTLEMENT_MODEL, UNKNOWN_SETTLEMENT_INTENT,
                                deadline_s=vendor_call.DEADLINE_CEIL_S).out_tok is None


def test_unreported_output_is_not_complete_zero_settlement():
    floor = pricing.realtime_cost(UNKNOWN_SETTLEMENT_MODEL, UNKNOWN_SETTLEMENT_INPUT, 0)
    call_id = calls.record_call("openai", UNKNOWN_SETTLEMENT_MODEL, "realtime", None,
                                in_tok=UNKNOWN_SETTLEMENT_INPUT, out_tok=None,
                                input_cost_floor=pricing.input_cost_lower_bound(
                                    UNKNOWN_SETTLEMENT_MODEL, UNKNOWN_SETTLEMENT_INPUT, provider="openai"),
                                intent=UNKNOWN_SETTLEMENT_INTENT)
    assert call_id, "incomplete call must be kept"
    connection = calls._calls_db()
    output, cost = connection.execute("SELECT out_tok,cost FROM calls WHERE id=?", (call_id,)).fetchone()
    assert output is None, "unreported output was persisted as a genuine zero"
    assert cost is None, "input floor was persisted as a complete total"
    complete, input_floor = connection.execute(
        "SELECT settlement_complete,input_cost_floor FROM calls WHERE id=?", (call_id,)).fetchone()
    assert complete == 0
    assert input_floor == pricing.input_cost_lower_bound(
        UNKNOWN_SETTLEMENT_MODEL, UNKNOWN_SETTLEMENT_INPUT, provider="openai")
    assert connection.execute(
        "SELECT actual_usd,counterfactual_usd,saved_usd FROM decisions WHERE call_id=?",
        (call_id,)).fetchone() == (None, None, None), "routing must retain unknown monetary evidence"

    # A reported zero is still a complete settlement, including genuinely free input.
    zero_id = calls.record_call("openai", UNKNOWN_SETTLEMENT_MODEL, "realtime", floor,
                                in_tok=UNKNOWN_SETTLEMENT_INPUT, out_tok=0,
                                intent=UNKNOWN_SETTLEMENT_INTENT)
    assert connection.execute("SELECT out_tok,cost,settlement_complete FROM calls WHERE id=?",
                              (zero_id,)).fetchone() == (0, floor, 1)

    # Backfill and batch reconciliation are writers of the same financial resource.
    backfill_id = calls.insert("openai", UNKNOWN_SETTLEMENT_MODEL, "batch", None,
                               in_tok=UNKNOWN_SETTLEMENT_INPUT, out_tok=None,
                               intent=UNKNOWN_SETTLEMENT_INTENT)
    assert connection.execute("SELECT out_tok,cost,settlement_complete FROM calls WHERE id=?",
                              (backfill_id,)).fetchone() == (None, None, 0)
    assert calls.reconcile_calls_batch_estimate("openai", UNKNOWN_SETTLEMENT_MODEL,
                                               UNKNOWN_SETTLEMENT_INTENT, UNKNOWN_SETTLEMENT_INPUT,
                                               None, None)
    assert connection.execute("SELECT out_tok,cost,settlement_complete FROM calls WHERE id=?",
                              (backfill_id,)).fetchone() == (None, None, 0)
    assert calls.reconcile_calls_batch_estimate("openai", UNKNOWN_SETTLEMENT_MODEL,
                                               UNKNOWN_SETTLEMENT_INTENT, UNKNOWN_SETTLEMENT_INPUT,
                                               0, floor)
    assert connection.execute("SELECT out_tok,cost,settlement_complete FROM calls WHERE id=?",
                              (backfill_id,)).fetchone() == (0, floor, 1)


def test_remote_reconstruction_unknown_tokens_never_produce_measured_value():
    import json
    from spendguard import resources, adapters, conv, expected_output, saas

    extracted = {"runs": [{"model": UNKNOWN_SETTLEMENT_MODEL, "in_tokens": UNKNOWN_SETTLEMENT_INPUT,
                           "out_tokens": None, "executed": True, "confidence": 100}]}
    with patch.object(conv, "remote_llm_excerpts", return_value=[(UNKNOWN_SETTLEMENT_INTENT, "")]), \
         patch.object(conv, "resolve", return_value={"org": UNKNOWN_SETTLEMENT_INTENT}), \
         patch.object(expected_output, "expect", return_value=(UNKNOWN_SETTLEMENT_INPUT, None)), \
         patch.object(saas, "identity_for_org", return_value=None), \
         patch.object(adapters, "call", return_value={"text": json.dumps(extracted)}):
        result = resources.reconstruct_remote_llm(run=True)
    assert result["rows"][0]["out_tokens"] is None
    assert result["rows"][0]["usd"] is None
    assert result["rows"][0]["token_usage_complete"] is False
    assert result["total"] is None
    assert result["by_org"][UNKNOWN_SETTLEMENT_INTENT] is None


if __name__ == "__main__":
    test_remote_reconstruction_unknown_tokens_never_produce_measured_value()
    test_advisor_unknown_tokens_never_produce_measured_value()
    test_lane_economics_unknown_token_metrics_stay_unknown()
    test_lane_quota_unknown_tokens_never_become_measured_average()
    test_decision_unknown_counterfactual_propagates_through_rollups()
    test_calibration_unknown_actual_tokens_stay_unknown()
    test_cost_integrity_unknown_tokens_have_no_manufactured_ceiling()
    test_measurement_producers_preserve_unknown_output_tokens()
    test_unreported_output_is_not_complete_zero_settlement()
    print("unknown output settlement guard passed")
