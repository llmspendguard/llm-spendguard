"""Authenticated ZCode coding-plan CLI executor for the zai agentic lane."""

import json
import pathlib
import shutil
import subprocess
import tempfile
import time
from ...store.configuration.config import cli_resolver


TIMEOUT_S = 300
ZCODE_BIN_ENV = "SPENDGUARD_ZCODE_BIN"
ZCODE_CONFIG_PATH = pathlib.Path.home() / ".zcode" / "cli" / "config.json"
ZCODE_COMPLETION_MODE = "plan"
ZCODE_AGENTIC_MODE = "build"
ZCODE_WORK_DIRECTORY_PREFIX = "spendguard-zcode-"


_bin = cli_resolver("zcode", ZCODE_BIN_ENV)


def available() -> bool:
    return _bin() is not None


def auth_status() -> dict:
    return {"authed": bool(_bin()) and ZCODE_CONFIG_PATH.exists()}


def run_prompt(prompt, system=None, model=None, timeout=TIMEOUT_S, reasoning=None, max_tokens=None,
               *, agentic=False, cwd=None) -> dict:
    """Run a headless ZCode turn; its coding plan selects the model and output budget."""
    from ... import execution_journal
    durable_id = execution_journal.current_durable_execution_id()
    process_runner = execution_journal.run_durable_delegate_subprocess if durable_id else subprocess.run
    executable = _bin()
    if agentic and cwd is None:
        return {"error": "agentic ZCode execution requires an explicit workspace"}
    if not executable:
        return {"error": "zcode CLI not found"}

    full_prompt = f"{system.strip()}\n\n{prompt}" if system else prompt
    mode = ZCODE_AGENTIC_MODE if agentic else ZCODE_COMPLETION_MODE
    command = [executable, "--mode", mode, "--json", "--prompt", full_prompt]
    if cwd:
        command += ["--cwd", cwd]

    from ... import config
    environment = config.lane_exec_env(executable)
    started_at = time.time()
    neutral_directory = None
    run_directory = cwd
    if not agentic:
        neutral_directory = tempfile.mkdtemp(prefix=ZCODE_WORK_DIRECTORY_PREFIX)
        run_directory = neutral_directory
    try:
        try:
            completed = process_runner(command, capture_output=True, text=True, timeout=timeout,
                                       env=environment, stdin=subprocess.DEVNULL, cwd=run_directory)
        except subprocess.TimeoutExpired:
            if durable_id:
                raise
            return {"error": f"zcode lane timeout ({timeout}s)"}
        except Exception as error:
            if durable_id:
                raise
            return {"error": str(error)[:200]}

        if completed.returncode != 0:
            return {"error": (completed.stderr or completed.stdout or "zcode exited non-zero").strip()[:200]}
        try:
            document = json.loads(completed.stdout)
            response_text = (document.get("response") or "").strip()
            usage = document.get("usage") or {}
            in_tok = int(usage.get("inputTokens") or 0)
            out_tok = int(usage.get("outputTokens") or 0)
        except (ValueError, TypeError, AttributeError):
            return {"error": "zcode produced no parseable response"}
        if not response_text:
            return {"error": "zcode produced no parseable response"}
        return {"text": response_text, "in_tok": in_tok, "out_tok": out_tok,
                "latency": time.time() - started_at, "error": None}
    finally:
        if neutral_directory is not None:
            shutil.rmtree(neutral_directory, ignore_errors=True)
