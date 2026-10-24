"""Offline proof that whole-job method planning mirrors per-call schema capability routing."""
import os
import sys
import tempfile

import pytest


os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="sg-wholejob-schema-")
os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"
os.environ.setdefault("SPENDGUARD_NO_AUTOINSTALL", "1")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from spendguard import adapters, config, guard, pricing, route_economics, route_horizon, whole_job  # noqa: E402


STRICT_PROVIDER_SENTINEL = "strict-provider-sentinel"
COMPAT_PROVIDER_SENTINEL = "compat-provider-sentinel"
STRICT_MODEL_SENTINEL = "strict-model-sentinel"
COMPAT_MODEL_SENTINEL = "compat-model-sentinel"
INTENT_SENTINEL = "schema-capability-intent"
METERED_GROUP_ESTIMATE = 0.625
LANE_GROUP_ESTIMATE = 0.0
BATCH_GROUP_ESTIMATE = 0.125
TOKEN_ESTIMATE = 1
HORIZON_TASK_COUNT = 2
HORIZON_INPUT_TOKENS_PER_TASK = 2
HORIZON_OUTPUT_TOKENS_PER_TASK = 3
HORIZON_TOTAL_TOKENS = HORIZON_TASK_COUNT * (HORIZON_INPUT_TOKENS_PER_TASK + HORIZON_OUTPUT_TOKENS_PER_TASK)
HORIZON_METERED_COST_PER_TASK = METERED_GROUP_ESTIMATE / HORIZON_TASK_COUNT
HORIZON_WHY_SENTINEL = "controlled horizon allocation"
HORIZON_JOB_ID_PREFIX = "horizon"
OMITTED_SCHEMA_SENTINEL = object()
STRICT_SCHEMA = {"type": "object", "required": ["answer"], "properties": {"answer": {"type": "string"}}}
LENIENT_SCHEMA = {"type": "object", "properties": {"answer": {"type": "string"}}}


def _schema_capability_route_report(model, path, usd):
    return {"recommend": {"path": path, "usd": usd},
            "resolved": {"batch_model": model},
            "tokens": {"in_per_task": TOKEN_ESTIMATE, "out_per_task": TOKEN_ESTIMATE}}


def _schema_capability_horizon_report(method, usd, batch_tokens, realtime_model, batch_model):
    def _controlled_horizon_report(groups):
        assert len(groups) == 1
        group = groups[0]
        assert group["n"] == HORIZON_TASK_COUNT
        assert group["in_tok"] == HORIZON_INPUT_TOKENS_PER_TASK
        return {
            "allocations": [{
                "intent": group["intent"], "urgency": route_horizon.DEFERRABLE,
                "n": group["n"], "tokens": HORIZON_TOTAL_TOKENS, "method": method,
                "lane_free_tok": 0, "lane_eff_tok": 0, "batch_tok": batch_tokens,
                "meter_tok": 0, "unpriced_tok": 0, "usd": usd, "unpriced": False,
            }],
            "resolved": {"realtime_model": realtime_model, "batch_model": batch_model},
            "why": HORIZON_WHY_SENTINEL,
        }
    return _controlled_horizon_report


def _assert_horizon_metered_token_basis(model, in_tok, out_tok):
    assert model == STRICT_MODEL_SENTINEL
    assert in_tok == HORIZON_INPUT_TOKENS_PER_TASK
    assert out_tok == HORIZON_OUTPUT_TOKENS_PER_TASK
    return HORIZON_METERED_COST_PER_TASK


def _horizon_jobs(schema=STRICT_SCHEMA, **job_options):
    prompt = "p" * (HORIZON_INPUT_TOKENS_PER_TASK * 4)
    jobs = [{"id": f"{HORIZON_JOB_ID_PREFIX}-{index}", "intent": INTENT_SENTINEL, "prompt": prompt,
             **job_options} for index in range(HORIZON_TASK_COUNT)]
    if schema is not OMITTED_SCHEMA_SENTINEL:
        for job in jobs:
            job["schema"] = schema
    return jobs


def test_whole_job_schema_capability_filter(monkeypatch):
    decisions = []
    monkeypatch.setattr(adapters, "provider_for", lambda model: (
        COMPAT_PROVIDER_SENTINEL if model == COMPAT_MODEL_SENTINEL else STRICT_PROVIDER_SENTINEL))
    monkeypatch.setattr(adapters, "schema_capability", lambda provider: (
        adapters.SCHEMA_JSON_OBJECT if provider == COMPAT_PROVIDER_SENTINEL else adapters.SCHEMA_STRICT))
    monkeypatch.setattr(config, "advisor_model", lambda: STRICT_MODEL_SENTINEL)
    monkeypatch.setattr(config, "_cfg_get", lambda section, key, default=None: STRICT_MODEL_SENTINEL)
    monkeypatch.setattr(pricing, "realtime_cost", lambda model, in_tok, out_tok: METERED_GROUP_ESTIMATE)
    monkeypatch.setattr(guard, "record_decision", lambda **row: decisions.append(row))

    strict_jobs = [{"id": "strict", "intent": INTENT_SENTINEL, "prompt": "prompt", "schema": STRICT_SCHEMA}]
    monkeypatch.setattr(route_economics, "route_report", lambda *args, **kwargs:
                        _schema_capability_route_report(STRICT_MODEL_SENTINEL, "lane_only", LANE_GROUP_ESTIMATE))
    strict_plan = whole_job.plan_jobs(strict_jobs, {"urgency": "realtime"})
    strict_entry = strict_plan["plan"][0]
    assert strict_entry["method"] == "realtime"
    assert strict_entry["est_usd"] == METERED_GROUP_ESTIMATE
    assert decisions[-1]["basis"] == "capability"
    assert decisions[-1]["why"] == "strict-schema→enforce(set)"

    suppressed_plan = whole_job.plan_jobs(
        [{**strict_jobs[0], "no_metered_fallback": True}], {"urgency": "realtime"})
    suppressed_entry = suppressed_plan["plan"][0]
    assert suppressed_entry["method"] == "realtime"
    assert suppressed_entry["est_usd"] == LANE_GROUP_ESTIMATE
    assert "coverage caveat" in suppressed_entry["why"]
    assert decisions[-1]["why"] == "strict-schema→enforce-suppressed(set):no_metered_fallback"

    monkeypatch.setattr(route_economics, "route_report", lambda *args, **kwargs:
                        _schema_capability_route_report(COMPAT_MODEL_SENTINEL, "lane_only", LANE_GROUP_ESTIMATE))
    monkeypatch.setattr(config, "advisor_model", lambda: COMPAT_MODEL_SENTINEL)
    compat_plan = whole_job.plan_jobs(strict_jobs, {"urgency": "realtime"})
    assert compat_plan["plan"][0]["method"] == "realtime"
    assert compat_plan["plan"][0]["est_usd"] == LANE_GROUP_ESTIMATE

    monkeypatch.setattr(config, "advisor_model", lambda: STRICT_MODEL_SENTINEL)
    monkeypatch.setattr(route_economics, "route_report", lambda *args, **kwargs:
                        _schema_capability_route_report(STRICT_MODEL_SENTINEL, "lane_only", LANE_GROUP_ESTIMATE))
    no_schema = whole_job.plan_jobs(
        [{"id": "none", "intent": INTENT_SENTINEL, "prompt": "prompt"}], {"urgency": "realtime"})
    lenient = whole_job.plan_jobs(
        [{"id": "lenient", "intent": INTENT_SENTINEL, "prompt": "prompt", "schema": LENIENT_SCHEMA}],
        {"urgency": "realtime"})
    unchanged_fields = ("method", "est_usd", "why")
    assert tuple(no_schema["plan"][0][key] for key in unchanged_fields) == \
           tuple(lenient["plan"][0][key] for key in unchanged_fields)

    # The per-call router drops a prompt-only lane under this same pair of SSOT facts.  The set planner now chooses
    # the corresponding realtime execution leg and its metered estimate, eliminating plan/execution divergence.
    assert adapters.schema_capability(STRICT_PROVIDER_SENTINEL) == adapters.SCHEMA_STRICT
    assert strict_entry["method"] == "realtime" and strict_entry["est_usd"] > 0


def test_whole_job_enforcing_batch_remains_eligible(monkeypatch):
    monkeypatch.setattr(adapters, "provider_for", lambda model: STRICT_PROVIDER_SENTINEL)
    monkeypatch.setattr(adapters, "schema_capability", lambda provider: adapters.SCHEMA_STRICT)
    monkeypatch.setattr(config, "advisor_model", lambda: STRICT_MODEL_SENTINEL)
    monkeypatch.setattr(config, "_cfg_get", lambda section, key, default=None: STRICT_MODEL_SENTINEL)
    monkeypatch.setattr(guard, "record_decision", lambda **row: None)
    monkeypatch.setattr(route_economics, "route_report", lambda *args, **kwargs:
                        _schema_capability_route_report(STRICT_MODEL_SENTINEL, "batch_only", BATCH_GROUP_ESTIMATE))
    planned = whole_job.plan_jobs(
        [{"id": "batch", "intent": INTENT_SENTINEL, "prompt": "prompt", "schema": STRICT_SCHEMA}],
        {"urgency": "batch"})
    assert planned["plan"][0]["method"] == "batch"
    assert planned["plan"][0]["est_usd"] == BATCH_GROUP_ESTIMATE
    assert "configured batch leg is enforcing" in planned["plan"][0]["why"]


def _configure_horizon_capability_seams(monkeypatch, decisions):
    monkeypatch.setattr(adapters, "provider_for", lambda model: (
        COMPAT_PROVIDER_SENTINEL if model == COMPAT_MODEL_SENTINEL else STRICT_PROVIDER_SENTINEL))
    monkeypatch.setattr(adapters, "schema_capability", lambda provider: (
        adapters.SCHEMA_JSON_OBJECT if provider == COMPAT_PROVIDER_SENTINEL else adapters.SCHEMA_STRICT))
    monkeypatch.setattr(config, "advisor_model", lambda: STRICT_MODEL_SENTINEL)
    monkeypatch.setattr(config, "_cfg_get", lambda section, key, default=None: STRICT_MODEL_SENTINEL)
    monkeypatch.setattr(guard, "record_decision", lambda **row: decisions.append(row))


def test_horizon_strict_schema_forces_metered_realtime_with_exact_token_basis(monkeypatch):
    decisions = []
    _configure_horizon_capability_seams(monkeypatch, decisions)
    monkeypatch.setattr(pricing, "realtime_cost", _assert_horizon_metered_token_basis)
    monkeypatch.setattr(route_horizon, "horizon_report", _schema_capability_horizon_report(
        "lane", LANE_GROUP_ESTIMATE, 0, STRICT_MODEL_SENTINEL, COMPAT_MODEL_SENTINEL))

    entry = whole_job.plan_jobs(_horizon_jobs())["plan"][0]

    assert entry["method"] == "realtime"
    assert entry["est_usd"] == METERED_GROUP_ESTIMATE
    assert "lane/batch ineligible, metered realtime" in entry["why"]
    assert decisions[-1]["why"] == "strict-schema→enforce(set)"


def test_horizon_strict_schema_suppression_keeps_original_allocation(monkeypatch):
    for suppression_field in ("no_metered_fallback", "refuse_billed"):
        decisions = []
        _configure_horizon_capability_seams(monkeypatch, decisions)
        monkeypatch.setattr(pricing, "realtime_cost", _assert_horizon_metered_token_basis)
        monkeypatch.setattr(route_horizon, "horizon_report", _schema_capability_horizon_report(
            "lane", LANE_GROUP_ESTIMATE, 0, STRICT_MODEL_SENTINEL, COMPAT_MODEL_SENTINEL))

        entry = whole_job.plan_jobs(_horizon_jobs(**{suppression_field: True}))["plan"][0]

        assert entry["method"] == "realtime"
        assert entry["est_usd"] == LANE_GROUP_ESTIMATE
        assert "strict-schema→enforce-suppressed(set):no_metered_fallback (coverage caveat)" in entry["why"]
        assert decisions[-1]["why"] == "strict-schema→enforce-suppressed(set):no_metered_fallback"


def test_horizon_strict_schema_keeps_configured_enforcing_batch(monkeypatch):
    decisions = []
    _configure_horizon_capability_seams(monkeypatch, decisions)
    monkeypatch.setattr(route_horizon, "horizon_report", _schema_capability_horizon_report(
        "batch", BATCH_GROUP_ESTIMATE, HORIZON_TOTAL_TOKENS,
        COMPAT_MODEL_SENTINEL, STRICT_MODEL_SENTINEL))

    entry = whole_job.plan_jobs(_horizon_jobs())["plan"][0]

    assert entry["method"] == "batch"
    assert entry["est_usd"] == BATCH_GROUP_ESTIMATE
    assert "configured batch leg is enforcing" in entry["why"]
    assert decisions[-1]["why"] == "strict-schema→enforce(set)"


def test_horizon_lenient_and_absent_schema_are_exact_capability_noops(monkeypatch):
    decisions = []
    _configure_horizon_capability_seams(monkeypatch, decisions)
    monkeypatch.setattr(route_horizon, "horizon_report", _schema_capability_horizon_report(
        "lane", LANE_GROUP_ESTIMATE, 0, STRICT_MODEL_SENTINEL, COMPAT_MODEL_SENTINEL))
    expected_entry = {
        "intent": INTENT_SENTINEL,
        "n": HORIZON_TASK_COUNT,
        "method": "realtime",
        "est_usd": LANE_GROUP_ESTIMATE,
        "why": f"horizon ({route_horizon.DEFERRABLE} → lane): {HORIZON_WHY_SENTINEL}",
    }

    lenient_entry = whole_job.plan_jobs(_horizon_jobs(schema=LENIENT_SCHEMA))["plan"][0]
    absent_entry = whole_job.plan_jobs(_horizon_jobs(schema=OMITTED_SCHEMA_SENTINEL))["plan"][0]

    assert lenient_entry == expected_entry
    assert absent_entry == expected_entry
    assert decisions == []


def run_whole_job_schema_capability_checks():
    checks = (
        test_whole_job_schema_capability_filter,
        test_whole_job_enforcing_batch_remains_eligible,
        test_horizon_strict_schema_forces_metered_realtime_with_exact_token_basis,
        test_horizon_strict_schema_suppression_keeps_original_allocation,
        test_horizon_strict_schema_keeps_configured_enforcing_batch,
        test_horizon_lenient_and_absent_schema_are_exact_capability_noops,
    )
    for check in checks:
        monkeypatch = pytest.MonkeyPatch()
        try:
            check(monkeypatch)
        finally:
            monkeypatch.undo()
    print(f"OK test_whole_job_schema_capability: {len(checks)} checks passed")


if __name__ == "__main__":
    run_whole_job_schema_capability_checks()
