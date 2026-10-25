"""End-to-end install-hook regression using only isolated, offline state."""
import os
import subprocess
import sys
import tempfile
from pathlib import Path


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

        consumer_python = consumer_venv / "bin" / "python"
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


run_install_hook_end_to_end_regression()
print("[OK] install-hook completes and the consumer venv reports enforcing")
