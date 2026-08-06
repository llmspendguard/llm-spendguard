"""A seeded prediction, and a call the API never billed, are UNGRADEABLE — in the arithmetic, not just the UI.

WHY THIS GUARD EXISTS. The calibration harness gated `seeding` on the printed column only; the error itself
was still computed and written to the results file. The report then counted those rows and announced
"median |token error| 767%" — a large, confident number produced entirely by grading first observations
against themselves. Same failure family as counting base64 as tokens: nothing about the output said it was
meaningless. `score()` is the single place the decision is made, so it cannot diverge again.
"""
import importlib.util
import pathlib
import sys

_p = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "probe" / "estimator_calibration_run.py"
_spec = importlib.util.spec_from_file_location("estimator_calibration_run", _p)
calib = importlib.util.module_from_spec(_spec)
sys.modules["estimator_calibration_run"] = calib
_spec.loader.exec_module(calib)


def test_seeded_rows_carry_no_error_at_all():
    tok, usd, gradeable = calib.score(pred_out=71, act_out=1618, pred_usd=0.001, act_usd=0.03,
                                      seeding=True, unbilled_but_priced=False)
    assert gradeable is False
    assert tok is None, "a seeded prediction graded against its own first observation is not a measurement"
    assert usd is None, "the $ prediction derives from pred_out, so it is equally ungradeable"


def test_unbilled_but_priced_rows_carry_no_error():
    """A priced model that generated tokens for $0 came from a subscription lane, whose usage numbers are the
    CLI's session accounting — measured: in_tok=2 for a 66-token prompt. Not comparable, so not scored."""
    tok, usd, gradeable = calib.score(pred_out=100, act_out=500, pred_usd=0.01, act_usd=0.0,
                                      seeding=False, unbilled_but_priced=True)
    assert (tok, usd, gradeable) == (None, None, False)


def test_a_real_row_scores_exactly():
    tok, usd, gradeable = calib.score(pred_out=100, act_out=150, pred_usd=0.10, act_usd=0.20,
                                      seeding=False, unbilled_but_priced=False)
    assert gradeable is True
    assert tok == 50.0 and usd == 100.0


def test_a_billed_row_with_zero_output_is_not_scored_as_perfect():
    """Zero output is a failed call, not a prediction that came in low. Scoring it would credit the estimator
    for the model returning nothing — absence is unknown, never a value."""
    tok, usd, gradeable = calib.score(pred_out=100, act_out=0, pred_usd=0.10, act_usd=0.05,
                                      seeding=False, unbilled_but_priced=False)
    assert (tok, usd, gradeable) == (None, None, False)
