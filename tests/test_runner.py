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

import pytest

# Hard per-file wall budget. Calibration: slowest legit file ≈ 18s bare, but CI runs with COVERAGE
# tracing (~10× CPU on the heaviest modules) + `-n auto` contention on 2-vCPU runners — observed 48s
# for a file that takes 1.6s locally. 120s holds everywhere while still catching the incident-#25
# class (a 213s live-billing pull); accidental NETWORK is caught in milliseconds by the dead proxy
# below regardless — this budget is the backstop for sleeps and runaway loops.
FILE_BUDGET_S = 120

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
def test_script(script):
    env = dict(os.environ)
    env["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="sg-pytest-")
    env["SPENDGUARD_TEST_ISOLATED"] = "1"            # tests skip their own re-exec; use this isolated home
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
    r = subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=600, cwd=REPO)
    took = time.monotonic() - t0
    out = r.stdout + r.stderr
    assert r.returncode == 0, f"{os.path.basename(script)} exited {r.returncode}\n{out}"
    assert "[FAIL]" not in r.stdout and "FAIL:" not in r.stdout, f"{os.path.basename(script)} reported a failure\n{out}"
    # PER-FILE TIME BUDGET — the standing gate from incident #25: one file silently spent 213s on live
    # provider billing for weeks because slow ≠ red. Every run measures every file as a byproduct (no
    # separate profiler to remember to run); a hog now FAILS the suite the day it appears. Genuinely
    # heavy files raise the budget here, on purpose, in review — never by drifting.
    assert took <= FILE_BUDGET_S, (
        f"{os.path.basename(script)} took {took:.1f}s (> {FILE_BUDGET_S}s budget) — a sleep, live network "
        f"call, or runaway loop is hiding in an 'offline' test. Profile it: time python {script}")
