"""Computed recommendations survive optional dispatch failures; refusals do not."""
from contextlib import ExitStack
from unittest.mock import patch

from spendguard import (adapters, advisor, best_value, bulkgate, config, gate, intent_model_prior,
                        ledger_writer, models, pricing, submit)

CACHE_BOUNDARY_LABEL = "cache-writer-optional-boundary"
CACHE_BOUNDARY_MODEL = "cache-boundary-model"
CACHE_BOUNDARY_EFFORT = "cache-boundary-effort"
CACHE_BOUNDARY_AMOUNT = 1.0


def exercise_cache_writer_optional_boundaries():
    with ExitStack() as stack:
        stack.enter_context(patch.object(best_value, "_verdict_cache", {}))
        stack.enter_context(patch.object(best_value, "_evidence_fingerprint", return_value=CACHE_BOUNDARY_LABEL))
        stack.enter_context(patch.object(best_value, "_recall_bestvalue_verdict", return_value=None))
        stack.enter_context(patch.object(advisor, "recommend_models", return_value={
            "top": [{"id": CACHE_BOUNDARY_MODEL, "why": CACHE_BOUNDARY_LABEL}]}))
        stack.enter_context(patch.object(models, "effort_for", return_value=CACHE_BOUNDARY_EFFORT))
        stack.enter_context(patch.object(ledger_writer, "run_ledger_write", side_effect=OSError(CACHE_BOUNDARY_LABEL)))
        warning = stack.enter_context(patch.object(config, "warn_once"))
        result = best_value.select_model_effort(CACHE_BOUNDARY_LABEL, CACHE_BOUNDARY_MODEL)
        assert result["model"] == CACHE_BOUNDARY_MODEL
        assert result["effort"] == CACHE_BOUNDARY_EFFORT
        assert warning.called
        warning.reset_mock()
        assert intent_model_prior._store_intent_prior(CACHE_BOUNDARY_LABEL, CACHE_BOUNDARY_LABEL, []) is None
        assert warning.called
        stack.enter_context(patch.object(intent_model_prior, "_candidate_lines", return_value=(
            [CACHE_BOUNDARY_LABEL], {CACHE_BOUNDARY_MODEL}, {CACHE_BOUNDARY_MODEL: CACHE_BOUNDARY_AMOUNT})))
        stack.enter_context(patch.object(intent_model_prior, "_recall_intent_prior", return_value=None))
        stack.enter_context(patch.object(submit, "_count_tokens", return_value=len(CACHE_BOUNDARY_LABEL)))
        stack.enter_context(patch.object(pricing, "realtime_cost", return_value=CACHE_BOUNDARY_AMOUNT))
        stack.enter_context(patch.object(adapters, "call", return_value={"cost": CACHE_BOUNDARY_AMOUNT}))
        stack.enter_context(patch.object(adapters, "structured_reply", return_value={
            "top": [{"id": CACHE_BOUNDARY_MODEL, "why": CACHE_BOUNDARY_LABEL}]}))
        prior_result = intent_model_prior.rank_models_for_intent(CACHE_BOUNDARY_LABEL, run=True)
        assert prior_result["model"] == CACHE_BOUNDARY_MODEL
        assert prior_result["top"][0]["why"] == CACHE_BOUNDARY_LABEL
        try:
            bulkgate.record_estimate(CACHE_BOUNDARY_LABEL, CACHE_BOUNDARY_MODEL,
                                    CACHE_BOUNDARY_AMOUNT, CACHE_BOUNDARY_AMOUNT)
        except OSError:
            pass
        else:
            raise AssertionError("required authorization write swallowed admission failure")
    refusal = gate.deliberate_stop_types()[0](CACHE_BOUNDARY_LABEL)
    for recorder, arguments in (
        (best_value._store_bestvalue_verdict, (CACHE_BOUNDARY_LABEL, None, CACHE_BOUNDARY_LABEL, {})),
        (intent_model_prior._store_intent_prior, (CACHE_BOUNDARY_LABEL, CACHE_BOUNDARY_LABEL, [])),
    ):
        with patch.object(ledger_writer, "run_ledger_write", side_effect=refusal):
            try:
                recorder(*arguments)
            except type(refusal):
                pass
            else:
                raise AssertionError(f"{recorder.__name__} swallowed deliberate refusal")
    print("PASS: cache dispatch failures preserve computed recommendation; refusals and required failures propagate")


if __name__ == "__main__":
    exercise_cache_writer_optional_boundaries()
