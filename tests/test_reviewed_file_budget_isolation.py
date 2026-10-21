"""Fault-inject runner speed and elapsed time into the real offline budget gate."""
import importlib.util
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

REVIEWED_BUDGET_EXPECTED_GLOBAL_S = 120
REVIEWED_BUDGET_SLOW_SCALE = 2.0
REVIEWED_BUDGET_OVERRUN_S = 0.1
REVIEWED_BUDGET_CLOCK_ORIGIN_S = 0.0
REVIEWED_BUDGET_EXPECTED_PERF_MULTIPLIER = 2.0
REVIEWED_BUDGET_PERF_FILE = "test_reconcile_read_containment.py"
REVIEWED_BUDGET_ORDINARY_MULTIPLIER = 1.0
REVIEWED_BUDGET_HOG_EXTRA_FACTOR = 2.0
REVIEWED_BUDGET_RUNNER_MODULE_NAME = "reviewed_budget_runner_probe"
REVIEWED_BUDGET_CHILD_FAILURE_CODE = 1
REVIEWED_BUDGET_DISABLE_COVERAGE = "0"

reviewed_budget_runner_path = Path(__file__).with_name("test_runner.py")
reviewed_budget_runner_spec = importlib.util.spec_from_file_location(
    REVIEWED_BUDGET_RUNNER_MODULE_NAME, reviewed_budget_runner_path)
reviewed_budget_runner = importlib.util.module_from_spec(reviewed_budget_runner_spec)
with patch.dict(os.environ, {"SPENDGUARD_COVERAGE": REVIEWED_BUDGET_DISABLE_COVERAGE}):
    reviewed_budget_runner_spec.loader.exec_module(reviewed_budget_runner)
assert reviewed_budget_runner.FILE_BUDGET_S == REVIEWED_BUDGET_EXPECTED_GLOBAL_S
assert reviewed_budget_runner.REVIEWED_FILE_BUDGET_MULTIPLIERS == {
    REVIEWED_BUDGET_PERF_FILE: REVIEWED_BUDGET_EXPECTED_PERF_MULTIPLIER,
}


def exercise_reviewed_budget_elapsed_fault(script, elapsed_s, scale, child_returncode=0):
    """Exercise the production child checks and elapsed assertion without waiting."""
    reviewed_budget_child = SimpleNamespace(returncode=child_returncode, stdout="", stderr="")
    with patch.object(reviewed_budget_runner.subprocess, "run", return_value=reviewed_budget_child), \
            patch.object(reviewed_budget_runner.time, "monotonic",
                         side_effect=(REVIEWED_BUDGET_CLOCK_ORIGIN_S, elapsed_s)), \
            patch.object(reviewed_budget_runner.tempfile, "mkdtemp", return_value=os.environ["SPENDGUARD_HOME"]):
        reviewed_budget_runner.test_script(script, scale)


# Every hosted boundary uses calibrated reference seconds; only the measured
# full-ledger perf tier gets the reviewed workload multiplier.
for reviewed_budget_script in reviewed_budget_runner.SCRIPTS:
    reviewed_budget_multiplier = (REVIEWED_BUDGET_EXPECTED_PERF_MULTIPLIER
                                  if Path(reviewed_budget_script).name == REVIEWED_BUDGET_PERF_FILE
                                  else REVIEWED_BUDGET_ORDINARY_MULTIPLIER)
    reviewed_budget_expected_s = (REVIEWED_BUDGET_EXPECTED_GLOBAL_S * REVIEWED_BUDGET_SLOW_SCALE
                                  * reviewed_budget_multiplier)
    exercise_reviewed_budget_elapsed_fault(reviewed_budget_script, reviewed_budget_expected_s,
                                          REVIEWED_BUDGET_SLOW_SCALE)
    try:
        exercise_reviewed_budget_elapsed_fault(
            reviewed_budget_script, reviewed_budget_expected_s + REVIEWED_BUDGET_OVERRUN_S,
            REVIEWED_BUDGET_SLOW_SCALE)
    except AssertionError as reviewed_budget_error:
        assert "budget" in str(reviewed_budget_error)
        assert "sleep, live network" in str(reviewed_budget_error)
    else:
        raise AssertionError(f"runner accepted over-budget file {reviewed_budget_script}")
    reviewed_budget_hog_elapsed = (REVIEWED_BUDGET_EXPECTED_GLOBAL_S * reviewed_budget_multiplier
                                   * (reviewed_budget_runner.MAX_RUNNER_SCALE + REVIEWED_BUDGET_HOG_EXTRA_FACTOR))
    try:
        exercise_reviewed_budget_elapsed_fault(reviewed_budget_script, reviewed_budget_hog_elapsed,
                                              reviewed_budget_runner.MAX_RUNNER_SCALE)
    except AssertionError as reviewed_budget_max_scale_hog:
        assert "sleep, live network" in str(reviewed_budget_max_scale_hog)
    else:
        raise AssertionError(f"max-scale hog escaped for {reviewed_budget_script}")

reviewed_budget_probe_script = str(reviewed_budget_runner_path)
try:
    exercise_reviewed_budget_elapsed_fault(
        reviewed_budget_probe_script, REVIEWED_BUDGET_EXPECTED_GLOBAL_S,
        REVIEWED_BUDGET_SLOW_SCALE, REVIEWED_BUDGET_CHILD_FAILURE_CODE)
except AssertionError as reviewed_budget_failed_child:
    assert "exited" in str(reviewed_budget_failed_child)
else:
    raise AssertionError("calibrated budget concealed a failed child")
print("OK calibrated file budget isolation: all hosted boundaries and child failure preserved")
