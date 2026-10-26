"""Offline regression for a gate hook whose editable source path was dropped."""
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

if not os.environ.get("SPENDGUARD_TEST_ISOLATED"):
    os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"
    os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="sg-self-sufficient-hook-home-")
    os.execv(sys.executable, [sys.executable] + sys.argv)

from spendguard import setup  # noqa: E402


EDITABLE_PTH_FILE_NAME = "__editable__.llm_spendguard.pth"
GATE_HOOK_FAILURE_MARKER = "[spendguard-gate-hook] spendguard not installed:"
SITECUSTOMIZE_FAILURE_MARKER = "[sitecustomize] spendguard not installed:"
FAILCLOSED_IMPORT_MARKER = "[spendguard-gate-hook] gate could not arm"
DUMMY_GATED_PACKAGE_NAME = "spendguard_probe_gated_sdk"
DUMMY_NONGATED_PACKAGE_NAME = "spendguard_probe_nongated_sibling"
HOOK_PROBE = (
    "import json,sys;"
    "hook_path,src_path=sys.argv[1:];"
    "assert src_path not in sys.path;"
    "exec(open(hook_path).read());"
    "module=sys.modules.get('spendguard');"
    "print(json.dumps({'path_added':src_path in sys.path,'imported':module is not None,"
    "'has_install':hasattr(module,'install') if module else False,"
    "'has_require':hasattr(module,'require') if module else False}))"
)
FAILCLOSED_HOOK_PROBE = (
    "import importlib,json,sys;"
    "hook_path,gated_name,nongated_name=sys.argv[1:];"
    "exec(open(hook_path).read());"
    "blocked_message='';"
    "\ntry:\n importlib.import_module(gated_name)\n"
    "except ImportError as exc:\n blocked_message=str(exc)\n"
    "importlib.import_module(nongated_name);"
    "importlib.import_module('json');"
    "print(json.dumps({'blocked_message':blocked_message,'nongated_imported':True,'stdlib_imported':True}))"
)


def run_self_sufficient_gate_hook_regression():
    with tempfile.TemporaryDirectory(prefix="sg-self-sufficient-hook-") as scratch_directory:
        scratch_root = Path(scratch_directory)
        site_packages = scratch_root / "site-packages"
        site_packages.mkdir()
        source_directory = setup._pkg_src()
        editable_pth = site_packages / EDITABLE_PTH_FILE_NAME
        editable_pth.write_text(source_directory + "\n")
        gate_hook = site_packages / setup._PTH_NAME
        gate_hook.write_text(setup._PTH_HOOK)
        assert setup._PTH_SENTINEL in gate_hook.read_text()

        probe_environment = os.environ.copy()
        probe_environment.pop("PYTHONPATH", None)
        probe_environment["SPENDGUARD_HOME"] = os.fspath(scratch_root / "spendguard-home")
        probe_environment.pop("GATE_DISABLE", None)

        def probe_gate_hook(environment):
            result = subprocess.run(
                [sys.executable, "-S", "-c", HOOK_PROBE, os.fspath(gate_hook), source_directory],
                capture_output=True,
                text=True,
                check=True,
                cwd=scratch_root,
                env=environment,
            )
            assert not result.stderr, result.stderr
            return json.loads(result.stdout)

        armed = probe_gate_hook(probe_environment)
        assert armed == {"path_added": True, "imported": True, "has_install": True, "has_require": True}

        checkout_without_spendguard = scratch_root / "checkout-without-spendguard"
        checkout_without_spendguard.mkdir()
        for dummy_package_name in (DUMMY_GATED_PACKAGE_NAME, DUMMY_NONGATED_PACKAGE_NAME):
            dummy_package = scratch_root / dummy_package_name
            dummy_package.mkdir()
            (dummy_package / "__init__.py").touch()

        failing_pth_hook = site_packages / f"failing-{setup._PTH_NAME}"
        failing_pth_hook.write_text(setup._gate_hook_for_checkout_src(
            os.fspath(checkout_without_spendguard),
            gated_modules=[DUMMY_GATED_PACKAGE_NAME],
        ))
        failing_sitecustomize_hook = site_packages / "failing-sitecustomize.py"
        failing_sitecustomize_hook.write_text(setup._sitecustomize_hook_source(
            gated_modules=[DUMMY_GATED_PACKAGE_NAME]))

        def probe_failclosed_hook(hook_path, environment, surface_marker, expect_blocked):
            result = subprocess.run(
                [sys.executable, "-S", "-c", FAILCLOSED_HOOK_PROBE, os.fspath(hook_path),
                 DUMMY_GATED_PACKAGE_NAME, DUMMY_NONGATED_PACKAGE_NAME],
                capture_output=True,
                text=True,
                cwd=scratch_root,
                env=environment,
            )
            assert result.returncode == 0, result.stderr
            probe = json.loads(result.stdout)
            if expect_blocked:
                assert surface_marker in result.stderr, result.stderr
                assert FAILCLOSED_IMPORT_MARKER in probe["blocked_message"], probe
            else:
                assert not result.stderr, result.stderr
                assert not probe["blocked_message"], probe
            assert probe["nongated_imported"] is True
            assert probe["stdlib_imported"] is True

        for failing_hook, failure_marker in (
            (failing_pth_hook, GATE_HOOK_FAILURE_MARKER),
            (failing_sitecustomize_hook, SITECUSTOMIZE_FAILURE_MARKER),
        ):
            probe_failclosed_hook(
                failing_hook, probe_environment, failure_marker, expect_blocked=True)

        from spendguard.gate import gated_sdk_modules
        default_pth_hook_source = setup._gate_hook_for_checkout_src(
            os.fspath(checkout_without_spendguard))
        default_sitecustomize_source = setup._sitecustomize_hook_source()
        for gated_module_name in gated_sdk_modules():
            assert gated_module_name in default_pth_hook_source
            assert gated_module_name in default_sitecustomize_source

        gate_disabled_environment = probe_environment.copy()
        gate_disabled_environment["GATE_DISABLE"] = "1"
        disabled_by_environment = probe_gate_hook(gate_disabled_environment)
        assert disabled_by_environment == {
            "path_added": False, "imported": False, "has_install": False, "has_require": False}
        for failing_hook, failure_marker in (
            (failing_pth_hook, GATE_HOOK_FAILURE_MARKER),
            (failing_sitecustomize_hook, SITECUSTOMIZE_FAILURE_MARKER),
        ):
            probe_failclosed_hook(
                failing_hook, gate_disabled_environment, failure_marker, expect_blocked=False)

        persistent_home = Path(probe_environment["SPENDGUARD_HOME"])
        persistent_home.mkdir(exist_ok=True)
        (persistent_home / "disabled").touch()
        disabled_by_file = probe_gate_hook(probe_environment)
        assert disabled_by_file == {
            "path_added": False, "imported": False, "has_install": False, "has_require": False}
        for failing_hook, failure_marker in (
            (failing_pth_hook, GATE_HOOK_FAILURE_MARKER),
            (failing_sitecustomize_hook, SITECUSTOMIZE_FAILURE_MARKER),
        ):
            probe_failclosed_hook(
                failing_hook, probe_environment, failure_marker, expect_blocked=False)


run_self_sufficient_gate_hook_regression()
print("[OK] gate hook restores editable source path before importing spendguard")
