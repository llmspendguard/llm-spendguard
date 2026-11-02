"""A simulated slow runner needs the same scale used by promptness guards."""
import sys
from unittest.mock import patch

import _runner_budget_calibration as calibration
from _pytest_fixture_bridge import align_test_startup_home, run_pytest_style

align_test_startup_home(prefix=__name__)

# Import the actual migrated bound without doing a physical CPU calibration.
with patch.object(calibration, "measure_isolated_runner_budget_scale", return_value=(
        calibration.RUNNER_YARDSTICK_BASELINE_S, calibration.MIN_RUNNER_SCALE)):
    from test_ledger_ambient_wait_bounds import AMBIENT_WAIT_PROMPTNESS_S

PROMPTNESS_PROOF_MIDPOINT_DIVISOR = 2
PROMPTNESS_PROOF_CLOCK_ORIGIN = 0.0


def test_runner_scale_accepts_simulated_slow_promptness_and_rejects_unscaled_bound():
    simulated_yardstick = calibration.RUNNER_YARDSTICK_BASELINE_S * calibration.MAX_RUNNER_SCALE
    with patch.object(calibration, "runner_budget_cpu_yardstick"), patch.object(
            calibration.time, "monotonic", side_effect=(PROMPTNESS_PROOF_CLOCK_ORIGIN,
                                                       simulated_yardstick)):
        measured_elapsed, slow_scale = calibration.measure_isolated_runner_budget_scale()
    assert measured_elapsed == simulated_yardstick
    assert slow_scale == calibration.MAX_RUNNER_SCALE > calibration.MIN_RUNNER_SCALE
    scaled_bound = AMBIENT_WAIT_PROMPTNESS_S * slow_scale
    simulated_elapsed = (AMBIENT_WAIT_PROMPTNESS_S + scaled_bound) / PROMPTNESS_PROOF_MIDPOINT_DIVISOR
    assert simulated_elapsed <= scaled_bound, "scaled promptness must accept a valid slow response"
    assert not simulated_elapsed <= AMBIENT_WAIT_PROMPTNESS_S, "unscaled promptness falsely rejects it"
    with patch.object(calibration, "runner_budget_cpu_yardstick"), patch.object(
            calibration.time, "monotonic", side_effect=(PROMPTNESS_PROOF_CLOCK_ORIGIN,
                                                       calibration.RUNNER_YARDSTICK_BASELINE_S)):
        _, reference_scale = calibration.measure_isolated_runner_budget_scale()
    assert reference_scale == calibration.MIN_RUNNER_SCALE
    assert AMBIENT_WAIT_PROMPTNESS_S * reference_scale == AMBIENT_WAIT_PROMPTNESS_S


if __name__ == "__main__":
    sys.exit(run_pytest_style(globals()))
