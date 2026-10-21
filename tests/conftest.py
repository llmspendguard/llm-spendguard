"""Fail FAST on the wrong invocation instead of hanging.

The suite's canonical entry is bare `pytest` from the repo root: pyproject pins
`python_files = ["test_runner.py"]`, and the runner subprocess-runs every script-style test in an
isolated home. Pointing pytest at a script file directly (`pytest tests/test_x.py`) bypasses that
pin — the script's isolation `os.execv` then hijacks or hangs the pytest process (cost: an 8-minute
silent hang + several redundant full-suite runs on 2026-07-11). This hook turns that mistake into an
immediate, explanatory error."""
import os
import importlib.util
from pathlib import Path

import pytest

RUNNER_CALIBRATION_MODULE_NAME = "spendguard_test_runner_calibration"
RUNNER_CALIBRATION_WORKER_KEY = "spendguard_runner_budget_scale"
RUNNER_CALIBRATION_COVERAGE_ENV = "SPENDGUARD_COVERAGE"
RUNNER_CALIBRATION_COVERAGE_DISABLED = "0"


def pytest_configure(config):
    """Only the controller measures speed; workers consume its exact result."""
    if hasattr(config, "workerinput"):
        config._spendguard_runner_budget_scale = config.workerinput[RUNNER_CALIBRATION_WORKER_KEY]
        return
    calibration_spec = importlib.util.spec_from_file_location(
        RUNNER_CALIBRATION_MODULE_NAME, Path(__file__).with_name("test_runner.py"))
    calibration_module = importlib.util.module_from_spec(calibration_spec)
    prior_coverage = os.environ.get(RUNNER_CALIBRATION_COVERAGE_ENV)
    try:
        os.environ[RUNNER_CALIBRATION_COVERAGE_ENV] = RUNNER_CALIBRATION_COVERAGE_DISABLED
        calibration_spec.loader.exec_module(calibration_module)
    finally:
        if prior_coverage is None:
            os.environ.pop(RUNNER_CALIBRATION_COVERAGE_ENV, None)
        else:
            os.environ[RUNNER_CALIBRATION_COVERAGE_ENV] = prior_coverage
    config._spendguard_runner_budget_scale = calibration_module.measure_runner_budget_scale()


@pytest.hookimpl(optionalhook=True)
def pytest_configure_node(node):
    """Distribute controller calibration without calibrating contended workers."""
    node.workerinput[RUNNER_CALIBRATION_WORKER_KEY] = node.config._spendguard_runner_budget_scale


@pytest.fixture(scope="session")
def runner_budget_scale(request):
    return request.config._spendguard_runner_budget_scale


def pytest_collectstart(collector):
    name = os.path.basename(getattr(collector, "name", "") or "")
    if name.startswith("test_") and name.endswith(".py") and name not in ("test_runner.py", "test_chaos_runner.py"):
        raise pytest.UsageError(
            f"{name} is a script-style test — never collect it directly (its isolation re-exec "
            f"hijacks pytest). Run `pytest` bare (the runner picks it up), or run the script itself: "
            f"python tests/{name}"
        )
