"""Deterministic clock faults and executable old-comparison mutation control."""
import ast
import importlib.util
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

CALIBRATION_GUARD_MODULE_NAME = "runner_speed_calibration_probe"
CALIBRATION_GUARD_CLOCK_START_S = 0.0
CALIBRATION_GUARD_FAST_RATIO = 0.5
CALIBRATION_GUARD_SLOW_RATIO = 2.0
CALIBRATION_GUARD_CLAMP_EXCESS = 2.0
CALIBRATION_GUARD_COVERAGE_OFF = "0"
CALIBRATION_GUARD_FIXED_COMPARE = "took <= FILE_BUDGET_S"
CALIBRATION_GUARD_COMPARE_NAME = "file_budget_s"
CALIBRATION_GUARD_CONFTEST_MODULE_NAME = "runner_calibration_conftest_probe"
CALIBRATION_GUARD_INVALID_MEASUREMENTS = (0.0, -1.0, float("nan"), float("inf"))
CALIBRATION_GUARD_YARDSTICK_ITERATIONS = 10_000

calibration_guard_path = Path(__file__).with_name("test_runner.py")
calibration_guard_spec = importlib.util.spec_from_file_location(CALIBRATION_GUARD_MODULE_NAME, calibration_guard_path)
calibration_guard_runner = importlib.util.module_from_spec(calibration_guard_spec)
with patch.dict(os.environ, {"SPENDGUARD_COVERAGE": CALIBRATION_GUARD_COVERAGE_OFF}):
    calibration_guard_spec.loader.exec_module(calibration_guard_runner)
calibration_guard_runner.RUNNER_YARDSTICK_ITERATIONS = CALIBRATION_GUARD_YARDSTICK_ITERATIONS

# The whole kernel is deterministic, not just its timed wrapper.
assert calibration_guard_runner.runner_budget_cpu_yardstick() == calibration_guard_runner.runner_budget_cpu_yardstick()
for calibration_guard_invalid_elapsed in CALIBRATION_GUARD_INVALID_MEASUREMENTS:
    with patch.object(calibration_guard_runner.time, "monotonic", side_effect=(
            CALIBRATION_GUARD_CLOCK_START_S, calibration_guard_invalid_elapsed)):
        try:
            calibration_guard_runner.measure_runner_budget_scale()
        except calibration_guard_runner.RunnerBudgetCalibrationError:
            pass
        else:
            raise AssertionError("invalid elapsed calibration did not fail closed")
for calibration_guard_invalid_scale in CALIBRATION_GUARD_INVALID_MEASUREMENTS:
    try:
        calibration_guard_runner.test_script(str(calibration_guard_path), calibration_guard_invalid_scale)
    except calibration_guard_runner.RunnerBudgetCalibrationError:
        pass
    else:
        raise AssertionError("invalid budget scale did not fail closed")

calibration_guard_conftest_spec = importlib.util.spec_from_file_location(
    CALIBRATION_GUARD_CONFTEST_MODULE_NAME, calibration_guard_path.with_name("conftest.py"))
calibration_guard_conftest = importlib.util.module_from_spec(calibration_guard_conftest_spec)
calibration_guard_conftest_spec.loader.exec_module(calibration_guard_conftest)
calibration_guard_controller = SimpleNamespace()
with patch.object(calibration_guard_runner, "measure_runner_budget_scale",
                  return_value=CALIBRATION_GUARD_SLOW_RATIO) as calibration_guard_measurement, \
        patch.object(calibration_guard_conftest.importlib.util, "module_from_spec",
                     return_value=calibration_guard_runner), \
        patch.object(calibration_guard_conftest.importlib.util, "spec_from_file_location",
                     return_value=SimpleNamespace(loader=SimpleNamespace(exec_module=lambda module: None))):
    calibration_guard_conftest.pytest_configure(calibration_guard_controller)
    calibration_guard_node = SimpleNamespace(config=calibration_guard_controller, workerinput={})
    calibration_guard_conftest.pytest_configure_node(calibration_guard_node)
    calibration_guard_worker = SimpleNamespace(workerinput=calibration_guard_node.workerinput)
    calibration_guard_conftest.pytest_configure(calibration_guard_worker)
    calibration_guard_measurement.assert_called_once_with()
assert calibration_guard_worker._spendguard_runner_budget_scale == CALIBRATION_GUARD_SLOW_RATIO
assert calibration_guard_conftest.runner_budget_scale.__wrapped__(
    SimpleNamespace(config=calibration_guard_worker)) == CALIBRATION_GUARD_SLOW_RATIO
for calibration_guard_ratio in (CALIBRATION_GUARD_FAST_RATIO, CALIBRATION_GUARD_SLOW_RATIO,
                                calibration_guard_runner.MAX_RUNNER_SCALE + CALIBRATION_GUARD_CLAMP_EXCESS):
    with patch.object(calibration_guard_runner.time, "monotonic", side_effect=(
            CALIBRATION_GUARD_CLOCK_START_S,
            calibration_guard_runner.RUNNER_YARDSTICK_BASELINE_S * calibration_guard_ratio)):
        calibration_guard_scale = calibration_guard_runner.measure_runner_budget_scale()
    assert calibration_guard_scale == min(calibration_guard_runner.MAX_RUNNER_SCALE,
                                         max(calibration_guard_runner.MIN_RUNNER_SCALE, calibration_guard_ratio))


def invoke_calibration_guard_child(probe_callable, elapsed_s):
    """Inject a completed child and wall time into the real assertion body."""
    with patch.object(calibration_guard_runner.subprocess, "run",
                      return_value=SimpleNamespace(returncode=0, stdout="", stderr="")), \
            patch.object(calibration_guard_runner.time, "monotonic",
                         side_effect=(CALIBRATION_GUARD_CLOCK_START_S, elapsed_s)), \
            patch.object(calibration_guard_runner.tempfile, "mkdtemp", return_value=os.environ["SPENDGUARD_HOME"]):
        probe_callable(str(calibration_guard_path), CALIBRATION_GUARD_SLOW_RATIO)


def require_calibration_guard_hog_refusal(probe_callable, elapsed_s):
    """A hog must retain the incident-25 diagnostic under either comparator."""
    try:
        invoke_calibration_guard_child(probe_callable, elapsed_s)
    except AssertionError as calibration_guard_hog_error:
        assert "sleep, live network" in str(calibration_guard_hog_error)
    else:
        raise AssertionError("hog escaped the budget gate")


calibration_guard_honest_s = calibration_guard_runner.FILE_BUDGET_S * CALIBRATION_GUARD_SLOW_RATIO
calibration_guard_hog_s = calibration_guard_runner.FILE_BUDGET_S * (
    calibration_guard_runner.MAX_RUNNER_SCALE + CALIBRATION_GUARD_CLAMP_EXCESS)
invoke_calibration_guard_child(calibration_guard_runner.test_script, calibration_guard_honest_s)
require_calibration_guard_hog_refusal(calibration_guard_runner.test_script, calibration_guard_hog_s)

# Mutate the complete production function's actual budget comparator in memory.
# This is syntax-directed parsing, not a meaning classifier; require exactly one
# known-shape comparator so a refactor cannot silently disable the mutation.
calibration_guard_tree = ast.parse(calibration_guard_path.read_text())
calibration_guard_function = next(node for node in calibration_guard_tree.body
                                if isinstance(node, ast.FunctionDef) and node.name == "test_script")
calibration_guard_targets = [node for node in ast.walk(calibration_guard_function)
                             if isinstance(node, ast.Compare) and isinstance(node.left, ast.Name)
                             and node.left.id == "took" and len(node.comparators) == 1
                             and isinstance(node.comparators[0], ast.Name)
                             and node.comparators[0].id == CALIBRATION_GUARD_COMPARE_NAME]
assert len(calibration_guard_targets) == 1
calibration_guard_targets[0].comparators[0] = ast.parse(CALIBRATION_GUARD_FIXED_COMPARE, mode="eval").body.comparators[0]
calibration_guard_mutation_tree = ast.Module(body=[calibration_guard_function], type_ignores=[])
ast.fix_missing_locations(calibration_guard_mutation_tree)
calibration_guard_namespace = dict(vars(calibration_guard_runner))
exec(compile(calibration_guard_mutation_tree, str(calibration_guard_path), "exec"), calibration_guard_namespace)
calibration_guard_mutant = calibration_guard_namespace["test_script"]
try:
    invoke_calibration_guard_child(calibration_guard_mutant, calibration_guard_honest_s)
except AssertionError as calibration_guard_mutation_error:
    assert "budget" in str(calibration_guard_mutation_error)
    print("MUTATION FAIL expected: old fixed comparator falsely refuses honest slow runner")
else:
    raise AssertionError("old fixed comparator survived the honest-heavy guard")
require_calibration_guard_hog_refusal(calibration_guard_mutant, calibration_guard_hog_s)
invoke_calibration_guard_child(calibration_guard_runner.test_script, calibration_guard_honest_s)
require_calibration_guard_hog_refusal(calibration_guard_runner.test_script, calibration_guard_hog_s)
print("RESTORED PASS: honest-heavy accepted, hog refused; deterministic fast/slow/clamp faults pass")
