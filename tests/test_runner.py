"""pytest entry point — runs every test_*.py script as an isolated subprocess.

The suite is written as standalone scripts (some self-isolate via os.execv); running each as a
subprocess with its own temp SPENDGUARD_HOME keeps them working under `pytest` without rewriting them,
and guarantees none touch the real ~/.spendguard. `pytest` from the repo root runs the whole suite.
"""
import os
import sys
import glob
import time
import atexit
import tempfile
import sysconfig
import subprocess
import math
import pathlib
import shutil

import conftest

import pytest

from _runner_budget_calibration import (
    MAX_RUNNER_SCALE,
    MIN_RUNNER_SCALE,
    RUNNER_YARDSTICK_BASELINE_S,  # noqa: F401 - public calibration test seam
    RUNNER_YARDSTICK_ITERATIONS,  # noqa: F401 - public calibration test seam
    RunnerBudgetCalibrationError,
    measure_isolated_runner_budget_scale,
    runner_budget_cpu_yardstick,  # noqa: F401 - public calibration test seam
)

# Reference-machine seconds; runner compute calibration scales this tight budget.
FILE_BUDGET_S = 120
RUNNER_SUBPROCESS_TIMEOUT_S = 600
RUNNER_ORDINARY_FILE_MULTIPLIER = 1.0
# Perf tier: unchanged full-ledger containment proof (120k+ rows). Coverage
# -n auto measured 176.465s versus 92.029s focused (1.918x contention).
# Controller CPU calibration precedes worker contention; retain a reviewed 2x
# workload multiplier, never an absolute seconds exception or global relaxation.
RECONCILE_CONTAINMENT_BUDGET_MULTIPLIER = 2.0
REVIEWED_FILE_BUDGET_MULTIPLIERS = {
    "test_reconcile_read_containment.py": RECONCILE_CONTAINMENT_BUDGET_MULTIPLIER,
}


def measure_runner_budget_scale():
    """Measure once on the controller before dispatching test workers."""
    yardstick_elapsed, measured_scale = measure_isolated_runner_budget_scale()
    print(f"runner calibration: yardstick={yardstick_elapsed:.6f}s scale={measured_scale:.3f} "
          f"effective_budget={FILE_BUDGET_S * measured_scale:.3f}s")
    return measured_scale

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, REPO)
from scripts.test.chunked_suite import (  # noqa: E402
    GATE_SHORT_TMPDIR_BASE,
    _child_env,
    validate_gate_broker_socket_path_capacity,
)

RUNNER_SCRATCH_HOME_PREFIX = "sg-pytest-"
RUNNER_SCRATCH_TMPDIR_PREFIX = "sg-pytest-tmp-"
RUNNER_SCRATCH_PREFIXES = (RUNNER_SCRATCH_HOME_PREFIX, RUNNER_SCRATCH_TMPDIR_PREFIX)
DELIBERATE_TEMP_LEAK_DIRECTORY_NAME = "deliberate-tempdir-leak"
SCRIPTS = sorted(
    f for f in glob.glob(os.path.join(HERE, "test_*.py"))
    if os.path.basename(f) != "test_runner.py"
)

# Optional coverage: SPENDGUARD_COVERAGE=1 measures each subprocess-isolated script. After pytest:
# coverage combine && coverage report. We use coverage's startup hook (below) rather than `coverage run` so
# code imported DURING interpreter startup is counted too.
COVERAGE = os.environ.get("SPENDGUARD_COVERAGE") == "1"
RCFILE = os.path.join(REPO, ".coveragerc")


def _enable_startup_coverage():
    """Drop a .pth that runs `coverage.process_startup()` at interpreter start. .pth files execute BEFORE
    sitecustomize, so on a gated venv (whose sitecustomize imports + install()s spendguard at startup) the
    tracer is already attached — gate.py / pricing.py / __init__.py import-time lines get counted instead of
    being missed by a later-attaching `coverage run`. Activated per-subprocess via COVERAGE_PROCESS_START.
    Returns the .pth path (removed atexit), or None if site-packages isn't writable (then we fall back to
    `coverage run`, which still works but undercounts startup imports)."""
    try:
        pth = os.path.join(sysconfig.get_paths()["purelib"], "_spendguard_cov_subprocess.pth")
        with open(pth, "w") as f:
            f.write("import coverage; coverage.process_startup()\n")
        atexit.register(lambda: os.path.exists(pth) and os.remove(pth))
        return pth
    except OSError:
        return None


_COV_PTH = _enable_startup_coverage() if COVERAGE else None


@pytest.mark.parametrize("script", SCRIPTS, ids=[os.path.basename(s) for s in SCRIPTS])
def test_script(script, runner_budget_scale):
    if not math.isfinite(runner_budget_scale) or not MIN_RUNNER_SCALE <= runner_budget_scale <= MAX_RUNNER_SCALE:
        raise RunnerBudgetCalibrationError(f"invalid runner scale: {runner_budget_scale!r}")
    scratch_home = tempfile.mkdtemp(prefix=RUNNER_SCRATCH_HOME_PREFIX, dir=GATE_SHORT_TMPDIR_BASE)
    scratch_tmpdir = None
    try:
        scratch_tmpdir = tempfile.mkdtemp(prefix=RUNNER_SCRATCH_TMPDIR_PREFIX, dir=GATE_SHORT_TMPDIR_BASE)
        validate_gate_broker_socket_path_capacity(scratch_home, scratch_tmpdir)
        env = _child_env(scratch_home, scratch_tmpdir, runner_budget_scale)
        if COVERAGE:
            env["COVERAGE_PROCESS_START"] = RCFILE
            # with the .pth hook → plain python (coverage starts at startup, traces sitecustomize imports);
            # without it → `coverage run` (attaches after startup, misses those import-time lines).
            cmd = ([sys.executable, script] if _COV_PTH
                   else [sys.executable, "-m", "coverage", "run", "-p", "--rcfile", RCFILE, script])
        else:
            cmd = [sys.executable, script]
        t0 = time.monotonic()
        r = subprocess.run(cmd, capture_output=True, text=True, env=env,
                           timeout=RUNNER_SUBPROCESS_TIMEOUT_S * runner_budget_scale, cwd=REPO)
    finally:
        shutil.rmtree(scratch_home, ignore_errors=True)
        if scratch_tmpdir is not None:
            shutil.rmtree(scratch_tmpdir, ignore_errors=True)
    took = time.monotonic() - t0
    out = r.stdout + r.stderr
    assert r.returncode == 0, f"{os.path.basename(script)} exited {r.returncode}\n{out}"
    assert "[FAIL]" not in r.stdout and "FAIL:" not in r.stdout, f"{os.path.basename(script)} reported a failure\n{out}"
    # PER-FILE TIME BUDGET — the standing gate from incident #25: one file silently spent 213s on live
    # provider billing for weeks because slow ≠ red. Every run measures every file as a byproduct (no
    # separate profiler to remember to run); a hog now FAILS the suite the day it appears. Genuinely
    # heavy files receive the same measured compute scaling as every ordinary file.
    file_budget_multiplier = REVIEWED_FILE_BUDGET_MULTIPLIERS.get(
        os.path.basename(script), RUNNER_ORDINARY_FILE_MULTIPLIER)
    file_budget_s = FILE_BUDGET_S * runner_budget_scale * file_budget_multiplier
    print(f"file budget: {os.path.basename(script)} elapsed={took:.3f}s effective_budget={file_budget_s:.3f}s")
    assert took <= file_budget_s, (
        f"{os.path.basename(script)} took {took:.1f}s (> {file_budget_s}s budget) — a sleep, live network "
        f"call, or runaway loop is hiding in an 'offline' test. Profile it: time python {script}")


def runner_scratch_entries():
    """Return the exact repo-owned runner entries in Python's configured temp directory."""
    temp_root = pathlib.Path(tempfile.gettempdir())
    return {entry.name for entry in temp_root.iterdir()
            if entry.name.startswith(RUNNER_SCRATCH_PREFIXES)}


def test_runner_subprocess_scratch_has_no_net_growth(runner_budget_scale):
    """A real runner-dispatched subprocess must leave no scratch after its result is collected."""
    scratch_before = runner_scratch_entries()
    test_script(__file__, runner_budget_scale)
    scratch_after = runner_scratch_entries()
    assert scratch_after == scratch_before, (
        "runner subprocess scratch survived result collection: "
        f"added={sorted(scratch_after - scratch_before)!r}, removed={sorted(scratch_before - scratch_after)!r}"
    )


def test_session_temp_leak_guard_rejects_deliberate_directory(tmp_path):
    """Mutation proof: a surviving directory must make the session guard fail."""
    initial_entries = {entry.name for entry in tmp_path.iterdir()}
    deliberate_leak = tmp_path / DELIBERATE_TEMP_LEAK_DIRECTORY_NAME
    deliberate_leak.mkdir()
    try:
        with pytest.raises(pytest.fail.Exception, match="session leaked spendguard-owned temporary entries"):
            conftest.assert_no_new_owned_temp_entries(initial_entries, tmp_path)
    finally:
        deliberate_leak.rmdir()
