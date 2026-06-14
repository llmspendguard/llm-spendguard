"""pytest entry point — runs every test_*.py script as an isolated subprocess.

The suite is written as standalone scripts (some self-isolate via os.execv); running each as a
subprocess with its own temp SPENDGUARD_HOME keeps them working under `pytest` without rewriting them,
and guarantees none touch the real ~/.spendguard. `pytest` from the repo root runs the whole suite.
"""
import os
import sys
import glob
import tempfile
import subprocess

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = sorted(
    f for f in glob.glob(os.path.join(HERE, "test_*.py"))
    if os.path.basename(f) != "test_runner.py"
)


@pytest.mark.parametrize("script", SCRIPTS, ids=[os.path.basename(s) for s in SCRIPTS])
def test_script(script):
    env = dict(os.environ)
    env["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="sg-pytest-")
    env["SPENDGUARD_TEST_ISOLATED"] = "1"            # tests skip their own re-exec; use this isolated home
    r = subprocess.run([sys.executable, script], capture_output=True, text=True, env=env, timeout=600)
    out = r.stdout + r.stderr
    assert r.returncode == 0, f"{os.path.basename(script)} exited {r.returncode}\n{out}"
    assert "[FAIL]" not in r.stdout and "FAIL:" not in r.stdout, f"{os.path.basename(script)} reported a failure\n{out}"
