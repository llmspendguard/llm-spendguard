"""End-to-end install-hook regression using only isolated, offline state."""
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from spendguard.setup import (
    _NO_BUILD_ISOLATION_OPTION,
    _consumer_editable_install_command,
    _declared_setuptools_build_requirement,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]
CONSUMER_VENV_DIRECTORY_NAME = "consumer-venv"
ISOLATED_SPENDGUARD_DIRECTORY_NAME = "spendguard-home"
FAKE_OPENAI_API_KEY = "offline-install-hook-openai-key"
FAKE_ANTHROPIC_API_KEY = "offline-install-hook-anthropic-key"
ENFORCING_DOCTOR_MARKER = "ENFORCING HERE: 🟢 YES"
STARTUP_GATE_MODULE_PROBE = "import sys; print('spendguard' in sys.modules)"
STARTUP_GATE_ACTIVE_OUTPUT = "True"
VENV_CONFIGURATION_FILE_NAME = "pyvenv.cfg"
SYSTEM_SITE_PACKAGES_ENABLED_SETTING = "include-system-site-packages = true"
SYSTEM_SITE_PACKAGES_DISABLED_SETTING = "include-system-site-packages = false"
BUILD_ISOLATION_FAILURE_MARKER = "No matching distribution found for setuptools"
EDITABLE_INSTALL_FAILURE_PREFIX = "editable build backend unavailable offline:"
PEP517_EDITABLE_INSTALL_OPTION = "--use-pep517"


def run_install_hook_end_to_end_regression():
    with tempfile.TemporaryDirectory(prefix="spendguard-install-hook-e2e-") as scratch_directory:
        scratch_root = Path(scratch_directory)
        consumer_venv = scratch_root / CONSUMER_VENV_DIRECTORY_NAME
        isolated_spendguard_home = scratch_root / ISOLATED_SPENDGUARD_DIRECTORY_NAME
        subprocess.run(
            [sys.executable, "-m", "venv", "--system-site-packages", os.fspath(consumer_venv)],
            check=True,
            cwd=PROJECT_ROOT,
        )
        isolated_environment = os.environ.copy()
        isolated_environment.update({
            "HOME": os.fspath(scratch_root),
            "SPENDGUARD_HOME": os.fspath(isolated_spendguard_home),
            "SPENDGUARD_TEST_ISOLATED": "1",
            "PIP_NO_INDEX": "1",
            "PIP_NO_DEPS": "1",
            "OPENAI_API_KEY": FAKE_OPENAI_API_KEY,
            "ANTHROPIC_API_KEY": FAKE_ANTHROPIC_API_KEY,
        })
        isolated_environment.pop("PIP_FIND_LINKS", None)
        consumer_python = consumer_venv / "bin" / "python"
        declared_build_requirement = _declared_setuptools_build_requirement(PROJECT_ROOT)
        importable_backend_probe = subprocess.run(
            [os.fspath(consumer_python), "-c", (
                "import importlib.metadata as m; import setuptools; "
                "print(m.version('setuptools'))")],
            capture_output=True,
            text=True,
            env=isolated_environment,
            cwd=PROJECT_ROOT,
        )
        assert importable_backend_probe.returncode == 0, (
            f"{EDITABLE_INSTALL_FAILURE_PREFIX} {declared_build_requirement} not importable "
            f"in {consumer_python}: {importable_backend_probe.stderr}")
        assert importable_backend_probe.stdout.strip() in declared_build_requirement.specifier

        isolated_build_reproduction = subprocess.run(
            [os.fspath(consumer_python), "-m", "pip", "install", "-e", os.fspath(PROJECT_ROOT),
             PEP517_EDITABLE_INSTALL_OPTION, "--no-cache-dir"],
            capture_output=True,
            text=True,
            env=isolated_environment,
            cwd=PROJECT_ROOT,
        )
        isolated_build_output = isolated_build_reproduction.stdout + isolated_build_reproduction.stderr
        assert isolated_build_reproduction.returncode != 0, isolated_build_output
        assert BUILD_ISOLATION_FAILURE_MARKER in isolated_build_output, isolated_build_output

        offline_install_command = _consumer_editable_install_command(
            consumer_python, PROJECT_ROOT, timeout=None, environment=isolated_environment)
        assert _NO_BUILD_ISOLATION_OPTION in offline_install_command
        install_result = subprocess.run(
            [sys.executable, "-m", "spendguard", "install-hook", "--venv", os.fspath(consumer_venv)],
            capture_output=True,
            text=True,
            env=isolated_environment,
            cwd=PROJECT_ROOT,
        )
        install_output = install_result.stdout + install_result.stderr
        assert install_result.returncode == 0, install_output
        assert "Traceback" not in install_output, install_output

        startup_probe_result = subprocess.run(
            [os.fspath(consumer_python), "-c", STARTUP_GATE_MODULE_PROBE],
            capture_output=True,
            text=True,
            env=isolated_environment,
            cwd=PROJECT_ROOT,
        )
        assert startup_probe_result.returncode == 0, startup_probe_result.stderr
        assert startup_probe_result.stdout.strip() == STARTUP_GATE_ACTIVE_OUTPUT, startup_probe_result.stdout

        venv_configuration = consumer_venv / VENV_CONFIGURATION_FILE_NAME
        venv_configuration_text = venv_configuration.read_text()
        assert SYSTEM_SITE_PACKAGES_ENABLED_SETTING in venv_configuration_text
        venv_configuration.write_text(venv_configuration_text.replace(
            SYSTEM_SITE_PACKAGES_ENABLED_SETTING,
            SYSTEM_SITE_PACKAGES_DISABLED_SETTING,
        ))
        doctor_result = subprocess.run(
            [os.fspath(consumer_python), "-m", "spendguard", "doctor"],
            capture_output=True,
            text=True,
            env=isolated_environment,
            cwd=PROJECT_ROOT,
        )
        doctor_output = doctor_result.stdout + doctor_result.stderr
        assert ENFORCING_DOCTOR_MARKER in doctor_output, doctor_output

        bare_venv = scratch_root / "bare-consumer-venv"
        subprocess.run([sys.executable, "-m", "venv", os.fspath(bare_venv)], check=True, cwd=PROJECT_ROOT)
        bare_python = bare_venv / "bin" / "python"
        subprocess.run(
            [os.fspath(bare_python), "-m", "pip", "uninstall", "--yes", declared_build_requirement.name],
            check=True,
            capture_output=True,
            text=True,
            cwd=PROJECT_ROOT,
        )
        try:
            _consumer_editable_install_command(
                bare_python, PROJECT_ROOT, timeout=None, environment=isolated_environment)
        except RuntimeError as backend_gap:
            assert str(backend_gap).startswith(EDITABLE_INSTALL_FAILURE_PREFIX), str(backend_gap)
            assert str(declared_build_requirement) in str(backend_gap), str(backend_gap)
            assert os.fspath(bare_python) in str(backend_gap), str(backend_gap)
        else:
            raise AssertionError("offline bare interpreter did not name its unavailable editable build backend")
        online_environment = isolated_environment.copy()
        online_environment.pop("PIP_NO_INDEX")
        bare_install_command = _consumer_editable_install_command(
            bare_python, PROJECT_ROOT, timeout=None, environment=online_environment)
        assert PEP517_EDITABLE_INSTALL_OPTION in bare_install_command
        assert _NO_BUILD_ISOLATION_OPTION not in bare_install_command


run_install_hook_end_to_end_regression()
print("[OK] install-hook completes and the consumer venv reports enforcing")
