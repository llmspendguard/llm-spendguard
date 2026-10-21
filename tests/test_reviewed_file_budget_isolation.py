"""Fault-inject elapsed time into the real runner without sleeping or live calls."""
import importlib.util
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

REVIEWED_BUDGET_EXPECTED_GLOBAL_S = 120
REVIEWED_BUDGET_EXPECTED_CONTAINMENT_S = 360
REVIEWED_BUDGET_HOSTED_OBSERVATION_S = 247.3
REVIEWED_BUDGET_OVERRUN_S = 0.1
REVIEWED_BUDGET_CONTAINMENT_FILE = "test_reconcile_read_containment.py"
REVIEWED_BUDGET_RUNNER_MODULE_NAME = "reviewed_budget_runner_probe"
REVIEWED_BUDGET_CHILD_FAILURE_CODE = 1
REVIEWED_BUDGET_DISABLE_COVERAGE = "0"

reviewed_budget_runner_path = Path(__file__).with_name("test_runner.py")
reviewed_budget_runner_spec = importlib.util.spec_from_file_location(
    REVIEWED_BUDGET_RUNNER_MODULE_NAME, reviewed_budget_runner_path)
reviewed_budget_runner = importlib.util.module_from_spec(reviewed_budget_runner_spec)
# Import only the assertion harness: a child must never install/remove the
# parent runner's process-wide coverage startup hook during this probe.
with patch.dict(os.environ, {"SPENDGUARD_COVERAGE": REVIEWED_BUDGET_DISABLE_COVERAGE}):
    reviewed_budget_runner_spec.loader.exec_module(reviewed_budget_runner)
assert reviewed_budget_runner.FILE_BUDGET_S == REVIEWED_BUDGET_EXPECTED_GLOBAL_S
assert reviewed_budget_runner.REVIEWED_FILE_BUDGET_OVERRIDES_S == {
    REVIEWED_BUDGET_CONTAINMENT_FILE: REVIEWED_BUDGET_EXPECTED_CONTAINMENT_S,
}


def exercise_reviewed_budget_elapsed_fault(script, elapsed_s, child_returncode=0):
    """Exercise the production assertion with a controlled child and clock."""
    reviewed_budget_child = SimpleNamespace(returncode=child_returncode, stdout="", stderr="")
    with patch.object(reviewed_budget_runner.subprocess, "run", return_value=reviewed_budget_child), \
            patch.object(reviewed_budget_runner.time, "monotonic", side_effect=(0.0, elapsed_s)), \
            patch.object(reviewed_budget_runner.tempfile, "mkdtemp", return_value=os.environ["SPENDGUARD_HOME"]):
        reviewed_budget_runner.test_script(script)


# Every collected file is checked through the actual assertion, including future
# files: adding an override or accidentally raising the default cannot hide here.
for reviewed_budget_script in reviewed_budget_runner.SCRIPTS:
    reviewed_budget_is_containment = Path(reviewed_budget_script).name == REVIEWED_BUDGET_CONTAINMENT_FILE
    reviewed_budget_expected_s = (REVIEWED_BUDGET_EXPECTED_CONTAINMENT_S if reviewed_budget_is_containment
                                  else REVIEWED_BUDGET_EXPECTED_GLOBAL_S)
    exercise_reviewed_budget_elapsed_fault(reviewed_budget_script, reviewed_budget_expected_s)
    try:
        exercise_reviewed_budget_elapsed_fault(
            reviewed_budget_script, reviewed_budget_expected_s + REVIEWED_BUDGET_OVERRUN_S)
    except AssertionError as reviewed_budget_error:
        assert "budget" in str(reviewed_budget_error)
    else:
        raise AssertionError(f"runner accepted over-budget file {reviewed_budget_script}")

reviewed_budget_containment_path = str(reviewed_budget_runner_path.with_name(REVIEWED_BUDGET_CONTAINMENT_FILE))
exercise_reviewed_budget_elapsed_fault(reviewed_budget_containment_path, REVIEWED_BUDGET_HOSTED_OBSERVATION_S)
try:
    exercise_reviewed_budget_elapsed_fault(reviewed_budget_containment_path, REVIEWED_BUDGET_HOSTED_OBSERVATION_S,
                                         REVIEWED_BUDGET_CHILD_FAILURE_CODE)
except AssertionError as reviewed_budget_failed_child:
    assert "exited" in str(reviewed_budget_failed_child)
else:
    raise AssertionError("reviewed budget concealed a failed containment proof")
print("OK reviewed file budget isolation: every file boundary and failed-child fault preserved")
