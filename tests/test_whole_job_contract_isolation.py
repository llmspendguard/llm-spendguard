"""Adversarial same-intent contracts survive planning and execution (offline).

The original first-non-null group schema/system assignment must fail this guard.
Distinct budgets, quality choices and deadlines are execution contracts, not routing hints.
"""
import copy
import os
import tempfile
from unittest.mock import patch

os.environ.setdefault("SPENDGUARD_HOME", tempfile.mkdtemp())
os.environ.setdefault("SPENDGUARD_NO_AUTOINSTALL", "1")

from spendguard import whole_job, lane_balance, route_economics, submit, adapters, config, pricing  # noqa: E402

WHOLE_JOB_ISOLATION_INTENT = "contract-isolation-fixture"
WHOLE_JOB_ISOLATION_ESTIMATE = 0.0
WHOLE_JOB_ISOLATION_SET_CAP = 50.0
WHOLE_JOB_ISOLATION_TIGHT_CAP = 1.0
WHOLE_JOB_ISOLATION_JOBS = [
    {"id": "strict", "prompt": "strict input", "intent": WHOLE_JOB_ISOLATION_INTENT,
     "schema": {"type": "object", "required": ["answer"], "properties": {"answer": {"type": "string"}},
                "additionalProperties": False},
     "system": "strict system", "quality_bar": True, "budget_usd": 20.0, "deadline_s": 90.0},
    {"id": "lenient", "prompt": "lenient input", "intent": WHOLE_JOB_ISOLATION_INTENT,
     "schema": {"type": "object"}, "system": "lenient system", "quality_bar": False,
     "reasoning": "low", "budget_usd": 0.5, "deadline_s": 60.0},
    {"id": "deadline", "prompt": "deadline input", "intent": WHOLE_JOB_ISOLATION_INTENT,
     "deadline_s": 0.25, "budget_usd": 2.0, "reasoning": "high"},
    {"id": "no-spend", "prompt": "no spend input", "intent": WHOLE_JOB_ISOLATION_INTENT,
     "budget_usd": 0.0, "quality_bar": False},
    {"id": "default", "prompt": "default input", "intent": WHOLE_JOB_ISOLATION_INTENT},
]
WHOLE_JOB_ISOLATION_EXECUTED = {}
WHOLE_JOB_ISOLATION_BATCHES = []


def whole_job_isolation_price_stub(*args, **kwargs):
    return {"recommend": {"path": "lane_only", "usd": WHOLE_JOB_ISOLATION_ESTIMATE},
            "tokens": {"out_per_task": WHOLE_JOB_ISOLATION_ESTIMATE}}


def whole_job_isolation_metered_price_stub(*args, **kwargs):
    return WHOLE_JOB_ISOLATION_ESTIMATE


def whole_job_isolation_execution_spy(tasks, intent, **kwargs):
    for job_id in tasks:
        assert job_id not in WHOLE_JOB_ISOLATION_EXECUTED, "duplicate execution"
        WHOLE_JOB_ISOLATION_EXECUTED[job_id] = {
            "intent": intent, "prompt": kwargs["prompt_for"](job_id),
            **{field: kwargs[field] for field in ("schema", "system", "reasoning", "budget_usd")},
            "deadline_s": kwargs.get("deadline_s"), "has_deadline": "deadline_s" in kwargs,
        }
    return {job_id: {"text": job_id} for job_id in tasks}


def whole_job_isolation_batch_spy(tasks, model, **kwargs):
    WHOLE_JOB_ISOLATION_BATCHES.append((copy.deepcopy(tasks), kwargs))
    return {"batch_id": model}


with (patch.object(route_economics, "route_report", whole_job_isolation_price_stub),
      patch.object(pricing, "realtime_cost", whole_job_isolation_metered_price_stub),
      patch.object(lane_balance, "bulk_delegate", whole_job_isolation_execution_spy)):
    # Reverse the group as an adversarial ordering: neither the first nor last sibling is a contract authority.
    for submitted in (WHOLE_JOB_ISOLATION_JOBS, list(reversed(WHOLE_JOB_ISOLATION_JOBS))):
        for set_cap in (WHOLE_JOB_ISOLATION_SET_CAP, WHOLE_JOB_ISOLATION_TIGHT_CAP):
            for urgency in ("realtime", "batch"):
                WHOLE_JOB_ISOLATION_EXECUTED.clear()
                original = copy.deepcopy(submitted)
                planned = whole_job.plan_jobs(submitted, {"urgency": urgency})
                assert list(planned["groups"].values()) == [original]
                assert all(entry["method"] == "realtime" for entry in planned["plan"])
                assert "strict-schema→enforce(set): lane/batch ineligible, metered realtime" in \
                       planned["plan"][0]["why"]
                result = whole_job.run_jobs(submitted, {"urgency": urgency, "budget_usd": set_cap,
                                                        "quality_bar": True},
                                            checkpoint=os.path.join(tempfile.mkdtemp(), "checkpoint"))
                assert submitted == original, "execution mutated a submitted contract"
                assert set(result["results"]) == {job["id"] for job in submitted}
                assert not result["receipt"]["named_gaps"]
                for job in submitted:
                    assert WHOLE_JOB_ISOLATION_EXECUTED[job["id"]] == {
                        "intent": job["intent"], "prompt": job["prompt"],
                        "schema": job.get("schema"), "system": job.get("system"),
                        "reasoning": ({"strict": "best-value", "lenient": "low", "deadline": "high",
                                       "no-spend": None, "default": "best-value"}[job["id"]]),
                        "budget_usd": min(job.get("budget_usd", set_cap), set_cap),
                        "deadline_s": job.get("deadline_s"), "has_deadline": "deadline_s" in job,
                    }, job["id"]
    # No own contract + no goal quality/budget must remain explicitly unconstrained, never inherit strict's.
    WHOLE_JOB_ISOLATION_EXECUTED.clear()
    whole_job.run_jobs(WHOLE_JOB_ISOLATION_JOBS, {"urgency": "realtime"},
                       checkpoint=os.path.join(tempfile.mkdtemp(), "checkpoint"))
    default_contract = WHOLE_JOB_ISOLATION_EXECUTED[WHOLE_JOB_ISOLATION_JOBS[-1]["id"]]
    assert all(default_contract[field] is None for field in ("schema", "system", "reasoning", "budget_usd"))
    assert not default_contract["has_deadline"]

# Schema/system-only batches remain eligible, but the absent schema may not borrow a sibling's.
with (patch.object(route_economics, "route_report", whole_job_isolation_price_stub),
      patch.object(adapters, "provider_for", return_value="openai"),
      patch.object(config, "advisor_model", return_value=WHOLE_JOB_ISOLATION_INTENT),
      patch.object(submit, "submit_chat_tasks", whole_job_isolation_batch_spy)):
    batch_jobs = [{field: value for field, value in job.items()
                   if field not in whole_job._WHOLE_JOB_REALTIME_CONSTRAINT_FIELDS}
                  for job in WHOLE_JOB_ISOLATION_JOBS]
    whole_job.run_jobs(batch_jobs, {"urgency": "batch"},
                       checkpoint=os.path.join(tempfile.mkdtemp(), "checkpoint"))
    batch_tasks, batch_options = WHOLE_JOB_ISOLATION_BATCHES.pop()
    assert batch_options["schema"] is None
    for batch_task, job in zip(batch_tasks, batch_jobs):
        assert batch_task.get("schema", batch_options["schema"]) == job.get("schema")
        assert batch_task.get("system") == job.get("system")
print("OK whole_job_contract_isolation: adversarial contracts and explicit defaults preserved")
