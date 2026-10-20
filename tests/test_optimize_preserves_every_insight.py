"""Offline contract guard: estimates and recommendations consume every insight.

The adapter is mocked: assertions verify evidence transport, not semantic judgement.
Restoring optimize's ins[:12] mutation must fail this guard.
"""
from unittest.mock import patch

from spendguard import advisor


LEGACY_INSIGHT_CUTOFF = 12
EXTRA_INSIGHT_COUNT = 3
INSIGHT_CONFIDENCE = 0.9
TEST_ADVISOR_MODEL = "advisor-evidence-fixture"
TEST_OPTIMIZE_INTENT = "evidence-coverage-fixture"
LATE_QUALITY_CONSTRAINT = "The cheapest candidate failed quality validation; retain the validated model."
OPTIMIZE_INSIGHT_ROWS = [
    (index, f"Independent measured lesson {index}", "fixture", INSIGHT_CONFIDENCE, "")
    for index in range(LEGACY_INSIGHT_CUTOFF + EXTRA_INSIGHT_COUNT)
]
OPTIMIZE_INSIGHT_ROWS.append(
    (len(OPTIMIZE_INSIGHT_ROWS), LATE_QUALITY_CONSTRAINT, "fixture", INSIGHT_CONFIDENCE, "")
)

with (
    patch.object(advisor.config, "advisor_model", return_value=TEST_ADVISOR_MODEL),
    patch.object(advisor, "_evidence_table", return_value=("fixture evidence", 1)),
    patch.object(advisor.learn, "insights", return_value=OPTIMIZE_INSIGHT_ROWS),
    patch.object(advisor, "_meta_spent_str", return_value="fixture"),
    patch.object(advisor, "_count_tokens", return_value=0) as optimize_token_counter,
    patch.object(advisor.pricing, "realtime_cost", return_value=0),
    patch("spendguard.ui.estimate_only"),
    patch("spendguard.adapters.call", return_value={
        "error": None, "text": "fixture recommendation", "cost": 0,
    }) as optimize_adapter_mock,
):
    advisor.optimize(intent=TEST_OPTIMIZE_INTENT)
    optimize_adapter_mock.assert_not_called()
    optimize_estimated_input = optimize_token_counter.call_args.args[0]
    advisor.optimize(intent=TEST_OPTIMIZE_INTENT, run=True)
    optimize_adapter_mock.assert_called_once()
    optimize_decision_input = optimize_adapter_mock.call_args.args[1]
    assert optimize_estimated_input == advisor._OPT_SYS + optimize_decision_input
    for _, optimize_expected_lesson, _, _, _ in OPTIMIZE_INSIGHT_ROWS:
        assert optimize_expected_lesson in optimize_decision_input, (
            f"Optimizer omitted insight evidence: {optimize_expected_lesson}"
        )
    assert LATE_QUALITY_CONSTRAINT in optimize_decision_input

print("ok — estimate and optimizer receive every insight, including the late quality constraint")
