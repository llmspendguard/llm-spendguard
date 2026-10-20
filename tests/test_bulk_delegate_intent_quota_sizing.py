"""Bulk quota admission reads the job's SQLite measurements and refuses before transport."""
import inspect
import sqlite3
from functools import partial
from types import SimpleNamespace
from unittest.mock import patch

import spendguard
from spendguard import adapters, calls, dispatch, lane_balance, lane_catalog, lane_economics, lane_quota_cost

spendguard.require()

BULK_INTENT_QUOTA_JOB = "test:bulk-intent-quota-job"
BULK_INTENT_QUOTA_OTHER_JOB = "test:bulk-intent-quota-other-job"
BULK_INTENT_QUOTA_LANE = "codex"
BULK_INTENT_QUOTA_PROVIDER = "openai"
BULK_INTENT_QUOTA_MODEL = "gpt-5"
BULK_INTENT_QUOTA_TOKENS = 100
BULK_INTENT_QUOTA_OTHER_TOKENS = 10000
BULK_INTENT_QUOTA_OUTPUT_BUDGET = 1024
BULK_INTENT_QUOTA_PROMPT = "offline bulk quota fixture"
BULK_INTENT_QUOTA_REFUSAL = "injected admission refusal before provider dispatch"
bulk_intent_quota_observed = []
bulk_intent_quota_source = sqlite3.connect(":memory:", check_same_thread=False)
bulk_intent_quota_source.execute(
    "CREATE TABLE calls (intent TEXT, resolved_lane TEXT, executor TEXT, in_tok INTEGER, out_tok INTEGER)")
for bulk_intent_quota_job, bulk_intent_quota_tokens in (
    (BULK_INTENT_QUOTA_JOB, BULK_INTENT_QUOTA_TOKENS),
    (BULK_INTENT_QUOTA_OTHER_JOB, BULK_INTENT_QUOTA_OTHER_TOKENS),
):
    bulk_intent_quota_source.executemany(
        "INSERT INTO calls VALUES (?, ?, ?, ?, ?)",
        [(bulk_intent_quota_job, BULK_INTENT_QUOTA_LANE, BULK_INTENT_QUOTA_LANE,
          bulk_intent_quota_tokens, 0)] * lane_quota_cost.MIN_SAMPLES_FOR_MEASURED)


def bulk_intent_quota_guarded_budget(model, prompt, **kwargs):
    """Replace unrelated output-budget learning, preserving call's forwarded intent."""
    bulk_intent_quota_raw_parameters = inspect.signature(adapters._call_once).parameters
    return adapters._call_once(model, prompt, **{
        **{key: value for key, value in kwargs.items() if key in bulk_intent_quota_raw_parameters},
        "max_tokens": BULK_INTENT_QUOTA_OUTPUT_BUDGET,
    })


def bulk_intent_quota_capture_refusal(observations, lane, projected_tokens, **kwargs):
    observations.append((lane, projected_tokens, kwargs["token_costs"]))
    raise lane_quota_cost.PlanQuotaRefusal(BULK_INTENT_QUOTA_REFUSAL)


def bulk_intent_quota_forbid_transport(*args, **kwargs):
    raise AssertionError("provider dispatch reached after injected admission refusal")


with (
    patch.object(calls, "_calls_db", return_value=bulk_intent_quota_source),
    patch.object(lane_balance, "_bulk_arms", return_value=[(BULK_INTENT_QUOTA_LANE, BULK_INTENT_QUOTA_MODEL)]),
    patch.object(lane_economics, "prompt_lane_reserved", return_value=False),
    patch.object(lane_catalog, "lane_provider", return_value=BULK_INTENT_QUOTA_PROVIDER),
    patch.object(lane_quota_cost, "apply_anthropic_avoidance", side_effect=lambda candidates, **kw: list(candidates)),
    patch.object(lane_quota_cost, "quota_preflight", return_value={}),
    patch.object(lane_quota_cost, "enforce_single_call_plan_fraction",
                 side_effect=partial(bulk_intent_quota_capture_refusal, bulk_intent_quota_observed)),
    patch.object(dispatch, "acquire_or_none", return_value=True),
    patch.object(dispatch, "release"),
    patch.object(dispatch, "holding", return_value=True),
    patch.object(adapters, "_lane_cooling", return_value=False),
    patch.object(adapters, "_lane_too_big", return_value=False),
    patch.object(adapters, "_lane_model_cooling", return_value=False),
    patch.object(adapters, "_lane_for", return_value=(BULK_INTENT_QUOTA_LANE,
                 SimpleNamespace(run_prompt=bulk_intent_quota_forbid_transport))),
    patch.object(adapters, "_call_guarded", side_effect=bulk_intent_quota_guarded_budget),
):
    bulk_intent_quota_expected = lane_quota_cost.measured_lane_token_cost(BULK_INTENT_QUOTA_JOB)[BULK_INTENT_QUOTA_LANE]
    bulk_intent_quota_global = lane_quota_cost.measured_lane_token_cost()[BULK_INTENT_QUOTA_LANE]
    assert bulk_intent_quota_expected["tokens_per_call"] != bulk_intent_quota_global["tokens_per_call"]
    bulk_intent_quota_results = lane_balance.bulk_delegate(
        [BULK_INTENT_QUOTA_PROMPT], BULK_INTENT_QUOTA_JOB,
        lanes=[BULK_INTENT_QUOTA_LANE], reasoning="low", force=True,
        record_route=False, hedge_ms=0, refuse_billed=True)
    assert len(bulk_intent_quota_results) == 1
    assert bulk_intent_quota_results[0]["error"] == BULK_INTENT_QUOTA_REFUSAL
    assert not bulk_intent_quota_results[0]["text"]
    assert bulk_intent_quota_observed == [(
        BULK_INTENT_QUOTA_LANE, BULK_INTENT_QUOTA_TOKENS,
        {BULK_INTENT_QUOTA_LANE: bulk_intent_quota_expected},
    )], (bulk_intent_quota_observed, bulk_intent_quota_global)
bulk_intent_quota_source.close()
print("[OK] bulk_delegate forwards intent to actual lane quota admission; refusal prevents transport")
