"""Durable lane-cost degradation remains conservative across hook processes."""
import pathlib
import multiprocessing
import tempfile
from unittest.mock import patch

import pytest

import spendguard
spendguard.require()
from spendguard import config, lane_quota_cost, ledger_read


DURABLE_DEGRADE_TEST_INTENT = "test:durable-lane-cost"
DURABLE_DEGRADE_TEST_LANE = "durable-test-lane"
DURABLE_DEGRADE_HIGH_TOKENS = 500.0
DURABLE_DEGRADE_LOW_TOKENS = 100.0
DURABLE_DEGRADE_SAMPLE_COUNT = lane_quota_cost.MIN_SAMPLES_FOR_MEASURED
DURABLE_DEGRADE_CALL_COUNT = 1
DURABLE_DEGRADE_TIMEOUT_BUDGET = ledger_read.DEFAULT_LEDGER_READ_TIME_BUDGET_S
DURABLE_DEGRADE_PROCESS_START_METHOD = "fork"
DURABLE_DEGRADE_PROCESS_JOIN_TIMEOUT = config.LEDGER_WRITE_TIMEOUT_S


def _durable_degrade_test_cost(tokens):
    return {DURABLE_DEGRADE_TEST_LANE: {
        "tokens_per_call": tokens,
        "samples": DURABLE_DEGRADE_SAMPLE_COUNT,
        "basis": "measured",
    }}


def _read_durable_degrade_in_cold_process(home, result_queue):
    with patch.object(config, "HOME", pathlib.Path(home)), \
            patch.object(ledger_read, "read_ledger_sql",
                         side_effect=ledger_read.LedgerReadTimeout(DURABLE_DEGRADE_TIMEOUT_BUDGET)):
        result_queue.put(lane_quota_cost.measured_lane_token_cost(DURABLE_DEGRADE_TEST_INTENT))


def test_durable_lane_cost_maxima_survive_cold_module_state_and_never_decrease():
    with tempfile.TemporaryDirectory(prefix="lane-cost-degrade-") as temporary_home:
        home = pathlib.Path(temporary_home)
        with patch.object(config, "HOME", home):
            lane_quota_cost._write_authoritative_lane_cost_maxima(
                DURABLE_DEGRADE_TEST_INTENT, _durable_degrade_test_cost(DURABLE_DEGRADE_HIGH_TOKENS))
            lane_quota_cost._write_authoritative_lane_cost_maxima(
                DURABLE_DEGRADE_TEST_INTENT, _durable_degrade_test_cost(DURABLE_DEGRADE_LOW_TOKENS))

            process_context = multiprocessing.get_context(DURABLE_DEGRADE_PROCESS_START_METHOD)
            result_queue = process_context.Queue()
            cold_reader = process_context.Process(
                target=_read_durable_degrade_in_cold_process, args=(home, result_queue))
            cold_reader.start()
            cold_reader.join(DURABLE_DEGRADE_PROCESS_JOIN_TIMEOUT)
            assert cold_reader.exitcode == 0
            degraded = result_queue.get()

            assert degraded.authoritative is False
            assert degraded[DURABLE_DEGRADE_TEST_LANE]["tokens_per_call"] == DURABLE_DEGRADE_HIGH_TOKENS
            assert degraded[DURABLE_DEGRADE_TEST_LANE]["basis"] == lane_quota_cost.DEGRADED_LANE_TOKEN_COST_BASIS
            with pytest.raises(lane_quota_cost.PlanQuotaRefusal):
                lane_quota_cost.quota_preflight(
                    {DURABLE_DEGRADE_TEST_LANE: DURABLE_DEGRADE_CALL_COUNT},
                    headroom_rows=[], token_costs=degraded)
            with pytest.raises(lane_quota_cost.PlanQuotaRefusal):
                lane_quota_cost.enforce_single_call_plan_fraction(
                    DURABLE_DEGRADE_TEST_LANE, DURABLE_DEGRADE_HIGH_TOKENS,
                    headroom_rows=[], token_costs=degraded)


def test_lane_cost_timeout_without_durable_evidence_remains_visible():
    with tempfile.TemporaryDirectory(prefix="lane-cost-degrade-empty-") as temporary_home:
        with patch.object(config, "HOME", pathlib.Path(temporary_home)), \
                patch.object(ledger_read, "read_ledger_sql",
                             side_effect=ledger_read.LedgerQueryDeadline(DURABLE_DEGRADE_TIMEOUT_BUDGET)):
            with pytest.raises(ledger_read.LedgerQueryDeadline):
                lane_quota_cost.measured_lane_token_cost(DURABLE_DEGRADE_TEST_INTENT)
