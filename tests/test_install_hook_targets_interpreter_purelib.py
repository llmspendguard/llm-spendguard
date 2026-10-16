"""Regression guards for interpreter-resolved install-hook placement and startup verification.

Offline and hermetic: every subprocess boundary is faked; no interpreter, pip, network, or LLM runs.
"""
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from contextlib import redirect_stdout
from io import StringIO
from unittest.mock import patch

if not os.environ.get("SPENDGUARD_TEST_ISOLATED"):
    os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"
    os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="sg-install-hook-purelib-")
    os.execv(sys.executable, [sys.executable] + sys.argv)

from spendguard import coverage, setup  # noqa: E402


def _venv():
    root = Path(tempfile.mkdtemp(prefix="sg-mongrel-"))
    (root / "bin").mkdir()
    (root / "bin" / "python").touch()
    stale = root / "lib" / "python3.11" / "site-packages"
    active = root / "lib" / "python3.14" / "site-packages"
    stale.mkdir(parents=True)
    active.mkdir(parents=True)
    return root, stale, active


def _result(stdout="", stderr="", returncode=0):
    return SimpleNamespace(stdout=stdout, stderr=stderr, returncode=returncode)


def _fake_run(active, startup=(True,), resolved=None,
              sdk_states=None):
    import json
    sdk_states = sdk_states or {name: "absent" for name in setup._GATED_SDKS}
    answers = iter(startup)

    def run(argv, **kwargs):
        code = argv[2] if len(argv) > 2 and argv[1] == "-c" else ""
        if "sysconfig.get_path('purelib')" in code:
            return _result(str(active) + "\n")
        if "sys.modules.get('spendguard')" in code:
            return _result(("1" if next(answers) else "0") + "\n")
        if "inspect.getfile(sitecustomize)" in code:
            return _result(str(resolved or active / "sitecustomize.py") + "\n")
        if "sdk_gate_states[sdk_name] = 'patched' if sdk_patched else 'present_ungated'" in code:
            return _result(json.dumps(sdk_states) + "\n")
        if "keys: openai=" in code:
            return _result("  keys: openai=MISSING, anthropic=MISSING\n")
        raise AssertionError(f"unexpected subprocess: {argv!r}")

    return run


def test_targets_interpreter_reported_purelib():
    venv, stale, active = _venv()
    with patch("subprocess.run", _fake_run(active)):
        assert setup.install_hook(str(venv), install_pkg=False) == 0
    assert (active / "sitecustomize.py").exists()
    assert not (stale / "sitecustomize.py").exists()


def test_inactive_startup_fails_without_success_claim():
    venv, _, active = _venv()
    out = StringIO()
    with patch("subprocess.run", _fake_run(active, startup=(False,),
                                           resolved=active / "sitecustomize.py")), redirect_stdout(out):
        assert setup.install_hook(str(venv), install_pkg=False) != 0
    out = out.getvalue()
    assert "that interpreter is now gated" not in out
    assert "startup gate INACTIVE" in out


def test_hook_fires_with_no_sdks_reports_wired_not_enforcing():
    venv, _, active = _venv()
    out = StringIO()
    with patch("subprocess.run", _fake_run(active)), redirect_stdout(out):
        assert setup.install_hook(str(venv), install_pkg=False) == 0
    out = out.getvalue()
    assert "nothing to patch" in out
    assert "doctor` will show NOT ENFORCING" in out
    assert "that interpreter is now gated" not in out


def test_hook_fires_with_patched_sdk_reports_gated():
    venv, _, active = _venv()
    out = StringIO()
    sdk_states = {name: "absent" for name in setup._GATED_SDKS}
    sdk_states["openai"] = "patched"
    with patch("subprocess.run", _fake_run(active, sdk_states=sdk_states)), redirect_stdout(out):
        assert setup.install_hook(str(venv), install_pkg=False) == 0
    assert "that interpreter is now gated" in out.getvalue()


def test_hook_fires_with_present_ungated_sdk_fails_loudly():
    venv, _, active = _venv()
    out = StringIO()
    sdk_states = {name: "absent" for name in setup._GATED_SDKS}
    sdk_states["openai"] = "present_ungated"
    with patch("subprocess.run", _fake_run(active, sdk_states=sdk_states)), redirect_stdout(out):
        assert setup.install_hook(str(venv), install_pkg=False) == 1
    out = out.getvalue()
    assert "present LLM SDK is NOT gated" in out
    assert "that interpreter is now gated" not in out


def test_shadow_installs_owned_pth_and_reverifies():
    venv, _, active = _venv()
    shadow = Path(tempfile.mkdtemp()) / "sitecustomize.py"
    with patch("subprocess.run", _fake_run(active, startup=(False, True), resolved=shadow)):
        assert setup.install_hook(str(venv), install_pkg=False) == 0
    pth = active / setup._PTH_NAME
    assert pth.exists()
    assert setup._SENTINEL in pth.read_text()


def test_uninstall_owns_both_hooks_and_refuses_foreign():
    venv, _, active = _venv()
    site_hook = active / "sitecustomize.py"
    pth_hook = active / setup._PTH_NAME
    site_hook.write_text(setup._HOOK)
    pth_hook.write_text(setup._PTH_HOOK)
    with patch("subprocess.run", _fake_run(active)):
        assert setup.install_hook(str(venv), uninstall=True, install_pkg=False) == 0
    assert not site_hook.exists() and not pth_hook.exists()

    site_hook.write_text("# user's hook; no ownership marker\n")
    with patch("subprocess.run", _fake_run(active)):
        assert setup.install_hook(str(venv), uninstall=True, install_pkg=False) != 0
    assert site_hook.exists()


def test_coverage_uses_only_interpreter_purelib():
    venv, stale, active = _venv()
    (stale / "sitecustomize.py").write_text(setup._HOOK)
    with patch.object(coverage.subprocess, "run", _fake_run(active)):
        assert coverage._is_gated(str(venv)) is False
        (active / "sitecustomize.py").write_text(setup._HOOK)
        assert coverage._is_gated(str(venv)) is True


test_targets_interpreter_reported_purelib()
test_inactive_startup_fails_without_success_claim()
test_hook_fires_with_no_sdks_reports_wired_not_enforcing()
test_hook_fires_with_patched_sdk_reports_gated()
test_hook_fires_with_present_ungated_sdk_fails_loudly()
test_shadow_installs_owned_pth_and_reverifies()
test_uninstall_owns_both_hooks_and_refuses_foreign()
test_coverage_uses_only_interpreter_purelib()
print("[OK] install-hook targets interpreter purelib and verifies startup enforcement")
