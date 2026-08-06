"""A published CEILING must never be used as an EXPECTATION while a measurement exists.

WHY THIS GUARD EXISTS. expect() went straight from "this call-class is measured" to "the model's published
output limit". opus and gpt-5.5 both publish 128,000 output tokens; their real answers measured 400-2,100.
So any class without history was estimated ~100x high, and estimate-first — the rail that is supposed to let
you approve a job before paying for it — became unusable exactly when it mattered most, on new work. Observed:
an 8-prompt replay estimated at $282 against $12 of real spend, and a 26-request job warned at ~$99.86.

The rungs are ordered measured-before-ceiling on purpose:
    learned (this class x model)  ->  model-history (this model, all classes)  ->  caller-cap  ->  model-max
A ceiling is a BOUND. An expectation is a MEASUREMENT. Two numbers, two jobs.
"""
from spendguard import bulkgate, expected_output, pricing


def test_a_model_with_measured_history_is_not_estimated_at_its_ceiling():
    for model in ("claude-opus-4-8", "gpt-5.5"):
        mo = bulkgate.model_outputs(model)
        if not mo or (mo.get("n") or 0) < expected_output.MIN_OBS:
            continue                                    # nothing measured here; the ceiling is honest
        tokens, basis = expected_output.expect(model)
        ceiling = pricing.max_output_tokens(model)
        assert basis == "model-history", f"{model}: measured {mo['n']} outputs but expect() said {basis!r}"
        assert tokens == int(mo["p90"]), f"{model}: must be the measured p90, got {tokens}"
        if ceiling:
            assert tokens < ceiling, (
                f"{model}: expectation {tokens} is not below the published ceiling {ceiling} — a ceiling used "
                f"as an expectation over-states by orders of magnitude")


def test_the_class_measurement_still_wins_over_the_model_measurement():
    """A distribution for THIS class is strictly better evidence than the model's average across all classes.
    Adding the broader rung must not demote the narrower one."""
    src = __import__("inspect").getsource(expected_output.expect)
    assert src.index("bulkgate.maxtokens(sig)") < src.index("bulkgate.model_outputs(model)"), \
        "the sig-level measurement must be consulted before the model-level one"


def test_a_callers_cap_still_bounds_every_measured_answer():
    """The caller cannot receive more than they allowed, so no rung may exceed their max_tokens."""
    tokens, _ = expected_output.expect("claude-opus-4-8", max_tokens=32)
    assert tokens <= 32


def test_model_outputs_excludes_truncated_samples():
    """A truncated response was cut AT its cap, so it measures the cap, not the work. Same censoring as
    maxtokens() — otherwise the more a model truncated, the lower its expectation would go."""
    src = __import__("inspect").getsource(bulkgate.model_outputs)
    assert "if not r[1]" in src, "model_outputs must drop truncated rows before taking percentiles"
