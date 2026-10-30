"""Offline build-tool provisioning for builders and arbitrary consumer interpreters."""
from pathlib import Path
import subprocess
import sys
import tomllib

PYTHON_BUILD_PIP_PROBE_COMMAND = (
    "-c", "import importlib.util, sys; sys.exit(importlib.util.find_spec('pip') is None)"
)
PYTHON_BUILD_PIP_BOOTSTRAP_COMMAND = ("-m", "ensurepip", "--upgrade")
PYTHON_BUILD_WHEELHOUSE_DIRECTORY = "distribution_build_wheels"
PYTHON_BUILD_PROJECT_FILENAME = "pyproject.toml"


def ensure_python_build_pip(python_executable=None, timeout=None):
    """Seed the target from CPython's bundled wheels; return whether pip was seeded."""
    python_executable = python_executable or sys.executable
    probe = subprocess.run([python_executable, *PYTHON_BUILD_PIP_PROBE_COMMAND],
                           capture_output=True, text=True, timeout=timeout, check=False)
    if not probe.returncode:
        return False
    if probe.returncode != 1:
        raise RuntimeError((probe.stderr or probe.stdout or "pip availability probe failed").strip())
    bootstrap = subprocess.run([python_executable, *PYTHON_BUILD_PIP_BOOTSTRAP_COMMAND],
                               capture_output=True, text=True, timeout=timeout, check=False)
    if bootstrap.returncode:
        raise RuntimeError((bootstrap.stderr or bootstrap.stdout or "offline pip bootstrap failed").strip())
    return True


def ensure_python_build_backend(checkout, python_executable=None, timeout=None):
    """Install declared build requirements from shipped wheels, never an index."""
    project = Path(checkout) / PYTHON_BUILD_PROJECT_FILENAME
    requirements = tomllib.loads(project.read_text())["build-system"]["requires"]
    wheelhouse = Path(__file__).with_name(PYTHON_BUILD_WHEELHOUSE_DIRECTORY)
    command = [python_executable or sys.executable, "-m", "pip", "install", "--upgrade", "--no-index",
               "--find-links", str(wheelhouse), *requirements]
    completed = subprocess.run(command, capture_output=True, text=True, timeout=timeout, check=False)
    if completed.returncode:
        raise RuntimeError((completed.stderr or completed.stdout or "offline build backend provisioning failed").strip())
