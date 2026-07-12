"""Fail FAST on the wrong invocation instead of hanging.

The suite's canonical entry is bare `pytest` from the repo root: pyproject pins
`python_files = ["test_runner.py"]`, and the runner subprocess-runs every script-style test in an
isolated home. Pointing pytest at a script file directly (`pytest tests/test_x.py`) bypasses that
pin — the script's isolation `os.execv` then hijacks or hangs the pytest process (cost: an 8-minute
silent hang + several redundant full-suite runs on 2026-07-11). This hook turns that mistake into an
immediate, explanatory error."""
import os

import pytest


def pytest_collectstart(collector):
    name = os.path.basename(getattr(collector, "name", "") or "")
    if name.startswith("test_") and name.endswith(".py") and name != "test_runner.py":
        raise pytest.UsageError(
            f"{name} is a script-style test — never collect it directly (its isolation re-exec "
            f"hijacks pytest). Run `pytest` bare (the runner picks it up), or run the script itself: "
            f"python tests/{name}"
        )
