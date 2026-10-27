"""Provider-reported partial usage reaches the incomplete ledger without spending."""
import os
import time
from types import SimpleNamespace
from unittest.mock import patch

from _pytest_fixture_bridge import align_test_startup_home

align_test_startup_home(prefix=__name__)
os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"

from spendguard import calls, gate, pricing, bedrock_adapter, litellm_adapter, vertex_adapter, http_capture, budget
from spendguard.ledger_read import LedgerSettlementIncomplete

PARTIAL_USAGE_MODEL = "gpt-5-nano"
PARTIAL_USAGE_INPUT = 100
PARTIAL_USAGE_CAP_INTENT = "partial-provider-cap-guard"
PARTIAL_USAGE_CAP_MULTIPLIER = 2


def test_partial_provider_observation_survives_to_ledger():
    for extractor, input_field, output_field in (
        (gate._act_oai_chat, "prompt_tokens", "completion_tokens"),
        (gate._act_oai_resp, "input_tokens", "output_tokens"),
        (gate._act_anth_msg, "input_tokens", "output_tokens"),
    ):
        usage = SimpleNamespace(**{input_field: PARTIAL_USAGE_INPUT})
        bearer = SimpleNamespace(usage=usage)
        assert extractor(bearer) == (PARTIAL_USAGE_INPUT, None)
        setattr(usage, output_field, 0)
        assert extractor(bearer) == (PARTIAL_USAGE_INPUT, 0)
    observed = {}
    gate._pull_usage(SimpleNamespace(usage=SimpleNamespace(input_tokens=PARTIAL_USAGE_INPUT)), observed)
    assert observed == {"in": PARTIAL_USAGE_INPUT}
    gate._pull_usage(SimpleNamespace(usage=SimpleNamespace(output_tokens=0)), observed)
    assert observed["out"] == 0
    assert bedrock_adapter._toks("Converse", {"usage": {"inputTokens": PARTIAL_USAGE_INPUT}})[1] is None
    assert litellm_adapter._usage({"usage": {"prompt_tokens": PARTIAL_USAGE_INPUT}})[1] is None
    assert vertex_adapter._toks(SimpleNamespace(usage_metadata=SimpleNamespace(
        prompt_token_count=PARTIAL_USAGE_INPUT)))[1] is None
    assert http_capture._usage_from_json({"usage": {"prompt_tokens": PARTIAL_USAGE_INPUT}})[2] is None
    assert http_capture._usage_from_json({"object": "list", "usage": {"prompt_tokens": PARTIAL_USAGE_INPUT}})[2] == 0
    gate._record_rt(PARTIAL_USAGE_MODEL, {}, PARTIAL_USAGE_INPUT, None)
    row = calls._calls_db().execute(
        "SELECT out_tok,cost,settlement_complete,input_cost_floor FROM calls ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert row[:3] == (None, None, 0)
    assert row[3] == pricing.input_cost_lower_bound(PARTIAL_USAGE_MODEL, PARTIAL_USAGE_INPUT)


def test_incomplete_provider_floor_cannot_disable_intent_cap():
    input_floor = pricing.input_cost_lower_bound(PARTIAL_USAGE_MODEL, PARTIAL_USAGE_INPUT)
    cap_above_known_floor = input_floor * PARTIAL_USAGE_CAP_MULTIPLIER
    with calls.context(intent=PARTIAL_USAGE_CAP_INTENT):
        gate._record_rt(PARTIAL_USAGE_MODEL, {}, PARTIAL_USAGE_INPUT, None)
    with patch("spendguard.config.intent_cap", return_value=cap_above_known_floor), patch.object(
        gate, "_allow", return_value=False
    ):
        try:
            gate._intent_door_gate(PARTIAL_USAGE_CAP_INTENT, 0, "openai", PARTIAL_USAGE_MODEL)
        except gate.SpendGateRefused as refusal:
            incomplete = refusal.__cause__
            assert isinstance(incomplete, LedgerSettlementIncomplete)
            assert incomplete.incomplete_count >= 1
            assert incomplete.input_cost_floor >= input_floor
        else:
            raise AssertionError("partial settlement bypassed the configured monetary cap")


def test_partial_settlement_refuses_global_caps_and_sdk_wrappers():
    for monetary_read in (
        budget.spent_today, budget.spent_month, budget.spent_all_time,
        lambda: budget.spent_by_job(PARTIAL_USAGE_CAP_INTENT), budget.exceeded,
    ):
        try:
            monetary_read()
        except LedgerSettlementIncomplete:
            pass
        else:
            raise AssertionError("global monetary reader returned a floor as a complete total")
    for budget_backend in ("sqlite", "memory"):
        with patch("spendguard.config.budget_backend", return_value=budget_backend), patch.object(
            gate, "_allow", return_value=False
        ):
            for precheck in (
                lambda: gate._budget_check(0, PARTIAL_USAGE_MODEL, "openai", "batch"),
                lambda: gate._rt_precheck_guard(lambda kw: (PARTIAL_USAGE_MODEL, PARTIAL_USAGE_INPUT, 0), {}),
            ):
                try:
                    precheck()
                except gate.SpendGateRefused as refusal:
                    assert isinstance(refusal.__cause__, LedgerSettlementIncomplete)
                else:
                    raise AssertionError("incomplete settlement escaped a batch/global/SDK precheck")


def test_input_floor_distinguishes_missing_cache_from_measured_zero():
    ordinary_input_cost = pricing.realtime_cost(PARTIAL_USAGE_MODEL, PARTIAL_USAGE_INPUT, 0)
    discounted_input_cost = pricing.realtime_cost(
        PARTIAL_USAGE_MODEL, PARTIAL_USAGE_INPUT, 0, PARTIAL_USAGE_INPUT)
    missing_cache_floor = pricing.input_cost_lower_bound(PARTIAL_USAGE_MODEL, PARTIAL_USAGE_INPUT)
    assert missing_cache_floor == min(ordinary_input_cost, discounted_input_cost)
    assert pricing.input_cost_lower_bound(
        PARTIAL_USAGE_MODEL, PARTIAL_USAGE_INPUT, cache_read_tok=0) == ordinary_input_cost
    incomplete_price_card = dict(pricing.price(PARTIAL_USAGE_MODEL))
    incomplete_price_card.pop("cached_in", None)
    with patch("spendguard.pricing.price", return_value=incomplete_price_card):
        assert pricing.input_cost_lower_bound(PARTIAL_USAGE_MODEL, PARTIAL_USAGE_INPUT) is None
    for measured_cache, expected_floor in ((None, missing_cache_floor), (0, ordinary_input_cost)):
        row_id = calls.record_call("openai", PARTIAL_USAGE_MODEL, "realtime", None,
                                   in_tok=PARTIAL_USAGE_INPUT, out_tok=None, cache_read_tok=measured_cache)
        assert calls._calls_db().execute(
            "SELECT input_cost_floor FROM calls WHERE id=?", (row_id,)).fetchone()[0] == expected_floor


def test_provider_cache_presence_survives_accounting_to_input_floor():
    for measured_cache in (None, 0):
        expected_floor = pricing.input_cost_lower_bound(
            PARTIAL_USAGE_MODEL, PARTIAL_USAGE_INPUT, cache_read_tok=measured_cache)
        usage = SimpleNamespace(prompt_tokens=PARTIAL_USAGE_INPUT)
        if measured_cache is not None:
            usage.prompt_tokens_details = SimpleNamespace(cached_tokens=measured_cache)
        response = SimpleNamespace(usage=usage)
        estimate = lambda kw: (PARTIAL_USAGE_MODEL, PARTIAL_USAGE_INPUT, 0)
        for streaming in (False, True):
            before_id = calls._calls_db().execute("SELECT MAX(rowid) FROM calls").fetchone()[0] or 0
            if streaming:
                list(gate._observe_stream(iter([response]), PARTIAL_USAGE_MODEL, {},
                                          estimate, time.time(), False))
            else:
                gate._rt_account(PARTIAL_USAGE_MODEL, {}, response, estimate, gate._act_oai_chat)
            row = calls._calls_db().execute(
                "SELECT rowid,out_tok,cost,settlement_complete,input_cost_floor,cache_read_tok "
                "FROM calls ORDER BY rowid DESC LIMIT 1").fetchone()
            assert row[0] > before_id
            assert row[1:4] == (None, None, 0)
            assert row[4:] == (expected_floor, measured_cache)


def test_provider_breadth_recorders_keep_reported_zero_input():
    client = SimpleNamespace(meta=SimpleNamespace(service_model=SimpleNamespace(service_name="bedrock-runtime")))
    for output_count in (None, 0):
        usage = {"prompt_tokens": 0}
        vertex_usage = SimpleNamespace(prompt_token_count=0)
        bedrock_usage = {"inputTokens": 0}
        if output_count is not None:
            usage["completion_tokens"] = output_count
            vertex_usage.candidates_token_count = output_count
            bedrock_usage["outputTokens"] = output_count
        with patch.object(gate, "_record_rt") as captured:
            litellm_adapter.record_litellm({"model": PARTIAL_USAGE_MODEL}, {"usage": usage})
            vertex_adapter._capture_vertex_result({"model": PARTIAL_USAGE_MODEL},
                                                   SimpleNamespace(usage_metadata=vertex_usage))
            wrapped = bedrock_adapter._wrap(lambda *args, usage=bedrock_usage: {"usage": usage})
            wrapped(client, "Converse", {"modelId": PARTIAL_USAGE_MODEL})
            assert captured.call_count == 3
            assert all(call.args[2:4] == (0, output_count) for call in captured.call_args_list)


if __name__ == "__main__":
    test_partial_provider_observation_survives_to_ledger()
    test_incomplete_provider_floor_cannot_disable_intent_cap()
    test_partial_settlement_refuses_global_caps_and_sdk_wrappers()
    test_input_floor_distinguishes_missing_cache_from_measured_zero()
    test_provider_cache_presence_survives_accounting_to_input_floor()
    test_provider_breadth_recorders_keep_reported_zero_input()
    print("partial provider usage guard passed")
