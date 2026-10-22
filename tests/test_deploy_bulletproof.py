"""Deploy repairs editable drift, isolates its gate, refuses promotion on failed repair, and ends with a loud verdict."""
import contextlib
import io
import os
import pathlib
import subprocess
import sys
import tempfile
from types import SimpleNamespace

_ISOLATION_KEY = "SPENDGUARD_TEST_ISOLATED"
_HOME_KEY = "SPENDGUARD_HOME"
if os.environ.get(_ISOLATION_KEY) != "1":
    child_environment = os.environ.copy()
    child_environment[_ISOLATION_KEY] = "1"
    child_environment[_HOME_KEY] = tempfile.mkdtemp(prefix="spendguard-deploy-bulletproof-")
    raise SystemExit(subprocess.run([sys.executable, __file__], env=child_environment).returncode)

os.environ.setdefault("SPENDGUARD_NO_AUTOINSTALL", "1")
_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))

from spendguard import cli, config, gate, release  # noqa: E402

failures = []


def check(label, condition):
    print(f"  [{'OK' if condition else 'FAIL'}] {label}")
    if not condition:
        failures.append(label)


original_stale_check = gate._stale_editable_install
original_subprocess = cli._deploy_execute_subprocess
try:
    drift_states = iter([(True, "current", "stale"), (False, "current", "current")])
    gate._stale_editable_install = lambda: next(drift_states)
    install_calls = []

    def repaired_install(command, **kwargs):
        install_calls.append((command, kwargs))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    cli._deploy_execute_subprocess = repaired_install
    aligned, reason = cli._deploy_align_active_editable_install(_REPO_ROOT)
    check("version drift takes the self-heal path and re-verifies aligned", aligned and not reason)
    check("self-heal uses the active interpreter and disables pip's cache",
          install_calls and install_calls[0][0][0] == sys.executable and
          "--no-cache-dir" in install_calls[0][0])

    gate._stale_editable_install = lambda: (True, "current", "stale")
    cli._deploy_execute_subprocess = lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout="", stderr="")
    release._tracked_dirty = lambda: False
    promoted = []
    release.promote_release = lambda **kwargs: promoted.append(kwargs)
    config.single_instance_lock = contextlib.contextmanager(lambda name: iter([True]))
    output = io.StringIO()
    with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
        deploy_rc = cli._dispatch(["deploy", "--no-gate", "--no-restart", "--no-refresh"])
    check("deploy refuses promotion when drift remains after self-heal", deploy_rc != 0 and not promoted)
    check("failed deploy ends with the loud pointer-not-advanced verdict",
          output.getvalue().splitlines()[-1].startswith("🔴 DEPLOY FAILED") and
          "pointer NOT advanced" in output.getvalue().splitlines()[-1])

    captured_gate = {}

    def hermetic_gate(command, **kwargs):
        captured_gate.update(kwargs)
        return SimpleNamespace(returncode=0)

    cli._deploy_execute_subprocess = hermetic_gate
    real_home = os.environ[_HOME_KEY]
    gate_rc = cli._deploy_run_hermetic_gate(_REPO_ROOT / "scripts" / "test" / "chunked_suite.py")
    check("gate receives a fresh child-only SPENDGUARD_HOME",
          gate_rc == 0 and captured_gate["env"][_HOME_KEY] != real_home and os.environ[_HOME_KEY] == real_home)

    success_output = io.StringIO()
    with contextlib.redirect_stdout(success_output):
        success_rc = cli._deploy_succeeded_verdict({"short": "sha", "describe": "description"})
    check("success verdict is exact, loud, and terminal", success_rc == 0 and
          success_output.getvalue().strip() == "🟢 DEPLOY SUCCEEDED — promoted sha (description)")
finally:
    gate._stale_editable_install = original_stale_check
    cli._deploy_execute_subprocess = original_subprocess

print(f"\n{'OK' if not failures else 'FAIL'} test_deploy_bulletproof: {len(failures)} failure(s)")
raise SystemExit(1 if failures else 0)
