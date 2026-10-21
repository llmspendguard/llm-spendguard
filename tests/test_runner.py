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

import pytest

# Reference-machine seconds; runner compute calibration scales this tight budget.
FILE_BUDGET_S = 120
RUNNER_YARDSTICK_ITERATIONS = 10_000_000
RUNNER_YARDSTICK_MODULUS = 1_000_000_007
# Measured 2026-10-21 on the reference dev machine, gated Python 3.11,
# time.monotonic around runner_budget_cpu_yardstick, without coverage tracing.
RUNNER_YARDSTICK_BASELINE_S = 0.3443317089986522
MIN_RUNNER_SCALE = 1.0
MAX_RUNNER_SCALE = 4.0
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


class RunnerBudgetCalibrationError(RuntimeError):
    """Calibration cannot establish a finite positive compute measurement."""


def runner_budget_cpu_yardstick():
    """Fixed pure integer CPU work: no I/O, randomness, sleeps or dependencies."""
    yardstick_accumulator = 0
    for yardstick_index in range(RUNNER_YARDSTICK_ITERATIONS):
        yardstick_accumulator = (
            yardstick_accumulator + yardstick_index * yardstick_index
        ) % RUNNER_YARDSTICK_MODULUS
    return yardstick_accumulator


def measure_runner_budget_scale():
    """Measure once on the controller before dispatching test workers."""
    yardstick_started = time.monotonic()
    runner_budget_cpu_yardstick()
    yardstick_elapsed = time.monotonic() - yardstick_started
    if not math.isfinite(yardstick_elapsed) or yardstick_elapsed <= 0:
        raise RunnerBudgetCalibrationError(f"invalid yardstick elapsed: {yardstick_elapsed!r}")
    if not math.isfinite(RUNNER_YARDSTICK_BASELINE_S) or RUNNER_YARDSTICK_BASELINE_S <= 0:
        raise RunnerBudgetCalibrationError("invalid reference yardstick baseline")
    measured_scale = max(MIN_RUNNER_SCALE, min(MAX_RUNNER_SCALE, yardstick_elapsed / RUNNER_YARDSTICK_BASELINE_S))
    print(f"runner calibration: yardstick={yardstick_elapsed:.6f}s scale={measured_scale:.3f} "
          f"effective_budget={FILE_BUDGET_S * measured_scale:.3f}s")
    return measured_scale

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
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
    env = dict(os.environ)
    env["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="sg-pytest-")
    env["SPENDGUARD_TEST_ISOLATED"] = "1"            # tests skip their own re-exec; use this isolated home
    # Deterministic short recovery schedules in the offline tier; chaos sets its own timing explicitly.
    env["SPENDGUARD_LANE_BACKOFF_S"] = "0"
    env["SPENDGUARD_LANE_QUEUE_DEADLINE_S"] = "0"
    # OFFLINE, ENFORCED: the suite once inherited real provider keys and `doctor`'s leak check silently
    # pulled 30 days of LIVE provider billing inside a "offline" test — 213s of network in one file.
    # A dead proxy makes any accidental external call fail in milliseconds (loud, not slow); localhost
    # servers tests spin up themselves stay reachable via no_proxy. Real keys are stripped — a test that
    # needs a key sets its own fake one.
    env["http_proxy"] = env["https_proxy"] = env["HTTP_PROXY"] = env["HTTPS_PROXY"] = "http://127.0.0.1:9"
    env["no_proxy"] = env["NO_PROXY"] = "localhost,127.0.0.1"
    for k in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "VAST_API_KEY", "GEMINI_API_KEY",
              "RUNPOD_API_KEY", "MODAL_TOKEN_ID", "MODAL_TOKEN_SECRET", "LAMBDA_API_KEY"):
        env.pop(k, None)
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
