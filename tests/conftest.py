"""Fail FAST on the wrong invocation instead of hanging.

The suite's canonical entry is bare `pytest` from the repo root: pyproject pins
`python_files = ["test_runner.py"]`, and the runner subprocess-runs every script-style test in an
isolated home. Pointing pytest at a script file directly (`pytest tests/test_x.py`) bypasses that
pin — the script's isolation `os.execv` then hijacks or hangs the pytest process (cost: an 8-minute
silent hang + several redundant full-suite runs on 2026-07-11). This hook turns that mistake into an
immediate, explanatory error."""
import os
import importlib.util
import tempfile
from pathlib import Path

import pytest

from _gate_shard_plan import (
    GATE_SHARD_FIRST_INDEX, GATE_SHARD_INDEX_ENV, GATE_SHARD_MANIFEST_FILENAME,
    GATE_SHARD_TIMINGS_ENV, GateShardEvidenceError, distribute_gate_shard_nodeids,
    read_gate_shard_manifest, verify_gate_shard_partition,
)

RUNNER_CALIBRATION_MODULE_NAME = "spendguard_test_runner_calibration"
RUNNER_CALIBRATION_WORKER_KEY = "spendguard_runner_budget_scale"
RUNNER_CALIBRATION_COVERAGE_ENV = "SPENDGUARD_COVERAGE"
RUNNER_CALIBRATION_COVERAGE_DISABLED = "0"
TEST_SESSION_TEMP_ENV = "TMPDIR"
TEST_SESSION_TEMP_ROOT_PREFIX = "spendguard-test-session-"
SPENDGUARD_OWNED_TEMP_PREFIXES = (
    "spendguard-antigravity-", "spendguard-chat-cookies-", "spendguard-codex-",
    "spendguard-kimi-", "spendguard-subscription-", "codex-home", "codexhome", "codexsess",
    "codex-authrace-", "fake-repo-", "fake-transcripts-", "repo-cli-", "ghlit-", "ghlit2-",
    "ghcap-", "__main__", "hr_stress_", "doctor-residency-refusal-", "wd-gitrepo-", "wd-notrepo-",
    "sg-auth-robust-", "sg-codexauth-", "sg-codexhome-", "spendguard-medA-",
)
LEGITIMATE_TEMP_CACHE_PREFIX_ALLOWLIST = (
    "fastembed_cache", "node-compile-cache", "data-gym-cache", "pytest-", "pytest-of-",
)


def new_owned_temp_entries(initial_entries, temp_root):
    """Return newly leaked spendguard/test entries, excluding known externally-owned caches."""
    current_entries = {entry.name for entry in Path(temp_root).iterdir()}
    new_entries = current_entries - set(initial_entries)
    return sorted(
        name for name in new_entries
        if name.startswith(SPENDGUARD_OWNED_TEMP_PREFIXES)
        and not name.startswith(LEGITIMATE_TEMP_CACHE_PREFIX_ALLOWLIST)
    )


def assert_no_new_owned_temp_entries(initial_entries, temp_root):
    leaked_entries = new_owned_temp_entries(initial_entries, temp_root)
    if leaked_entries:
        pytest.fail(f"session leaked spendguard-owned temporary entries: {leaked_entries}")


@pytest.fixture(scope="session", autouse=True)
def reject_spendguard_temp_leaks_at_session_end():
    session_temp_owner = tempfile.TemporaryDirectory(prefix=TEST_SESSION_TEMP_ROOT_PREFIX)
    temp_root = Path(session_temp_owner.name)
    prior_temp_env = os.environ.get(TEST_SESSION_TEMP_ENV)
    prior_tempfile_root = tempfile.tempdir
    os.environ[TEST_SESSION_TEMP_ENV] = str(temp_root)
    tempfile.tempdir = str(temp_root)
    initial_entries = {entry.name for entry in temp_root.iterdir()}
    try:
        yield
        assert_no_new_owned_temp_entries(initial_entries, temp_root)
    finally:
        tempfile.tempdir = prior_tempfile_root
        if prior_temp_env is None:
            os.environ.pop(TEST_SESSION_TEMP_ENV, None)
        else:
            os.environ[TEST_SESSION_TEMP_ENV] = prior_temp_env
        session_temp_owner.cleanup()


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


@pytest.hookimpl(tryfirst=True)
def pytest_collection_modifyitems(config, items):
    """Partition complete collection before optional -k/-m filtering; default unchanged."""
    configured_index = os.environ.get(GATE_SHARD_INDEX_ENV)
    if configured_index is None:
        return
    try:
        index = int(configured_index)
        manifest_path = os.environ.get(GATE_SHARD_TIMINGS_ENV,
            str(Path(__file__).resolve().parent.parent / GATE_SHARD_MANIFEST_FILENAME))
        manifest = read_gate_shard_manifest(manifest_path)
        if not GATE_SHARD_FIRST_INDEX <= index < manifest["shard_count"]:
            raise GateShardEvidenceError(f"shard index outside configured range: {configured_index}")
        nodeids = [item.nodeid for item in items]
        partitions = distribute_gate_shard_nodeids(nodeids, manifest)
        # Independently check the integration seam, even if the planner regresses.
        verify_gate_shard_partition(nodeids, partitions)
        selected = set(partitions[index])
        deselected = [item for item in items if item.nodeid not in selected]
        items[:] = [item for item in items if item.nodeid in selected]
        config.hook.pytest_deselected(items=deselected)
    except (GateShardEvidenceError, ValueError) as error:
        raise pytest.UsageError(f"gate shard evidence gap: {error}") from error


def pytest_collectstart(collector):
    name = os.path.basename(getattr(collector, "name", "") or "")
    if name.startswith("test_") and name.endswith(".py") and name not in ("test_runner.py", "test_chaos_runner.py"):
        raise pytest.UsageError(
            f"{name} is a script-style test — never collect it directly (its isolation re-exec "
            f"hijacks pytest). Run `pytest` bare (the runner picks it up), or run the script itself: "
            f"python tests/{name}"
        )
