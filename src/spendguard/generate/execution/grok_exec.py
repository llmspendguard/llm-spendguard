"""SuperGrok subscription lane driven by the authenticated Grok CLI."""
import json
import os
import pathlib
import shutil
import subprocess
import tempfile
import time

TIMEOUT_S = 300
GROK_HOME_ENV = "GROK_HOME"
GROK_HOME_DIRNAME = ".grok"
GROK_MODELS_CACHE_FILENAME = "models_cache.json"
GROK_WORK_DIRECTORY_PREFIX = "spendguard-grok-"
GROK_USAGE_SOURCE = "grok_cli"
GROK_USAGE_UNKNOWN_SAMPLE_COUNT = 0
GROK_REASONING_FLAG = "--reasoning-effort"
GROK_REASONING_DEFAULT = "low"               # lane default: Grok's floor and the chosen ~12s standard (lane_registry declares the same)
GROK_STANDARD_REASONING_FLOOR = "minimal"    # the STANDARD ordinal floor (reasoning_equivalence STANDARD_LEVELS[0]); Grok has no spelling below 'low'


def _bin():
    """Resolve the CLI by explicit pin, PATH, then configured user-local search directories."""
    from ... import config
    return config.resolve_cli("grok", "SPENDGUARD_GROK_BIN")


def available() -> bool:
    return _bin() is not None


def _grok_models_cache_path():
    home = pathlib.Path(os.environ.get(GROK_HOME_ENV) or pathlib.Path.home() / GROK_HOME_DIRNAME)
    return home / GROK_MODELS_CACHE_FILENAME


def _grok_model_records():
    """Read the CLI's live model cache; absent or malformed cache means no declared models."""
    try:
        document = json.loads(_grok_models_cache_path().read_text())
        models = document.get("models")
        return models if isinstance(models, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


def _grok_model_alias(model):
    """Exact requested-id lookup against the CLI cache; unknown ids fail closed."""
    requested = (model or "").split(":", 1)[-1].strip()
    if not requested:
        return None
    return requested if requested in _grok_model_records() else None


def usage():
    """The Grok CLI exposes no documented quota endpoint; availability remains non-blocking UNKNOWN."""
    return None


def grok_headroom_report():
    """Reader-wired UNKNOWN quota report without affecting lane servability."""
    return {"buckets": None, "source": GROK_USAGE_SOURCE, "reason": "grok_usage_endpoint_not_exposed",
            "n": GROK_USAGE_UNKNOWN_SAMPLE_COUNT, "window": None,
            "uncertainty": "provider_data_unknown"}


HEADROOM_REPORT_READER = grok_headroom_report


def _grok_effort(level):
    """Map a STANDARD ordinal reasoning level → the Grok CLI's own effort scale (like codex_exec._codex_effort).

    Grok ``-p`` accepts low|medium|high|xhigh and has NO spelling below ``low``, so the STANDARD floor
    ``minimal`` maps to Grok's floor ``low``; the rest pass through and the CLI validates them (a bad value
    fails fast). An omitted level uses the lane's ``low`` default (the ~12s standard). lane_registry declares
    the same default and level set for the surface/router, and the lane test asserts the two agree.
    """
    requested = (level or "").strip().lower()
    if not requested:
        return GROK_REASONING_DEFAULT
    return GROK_REASONING_DEFAULT if requested == GROK_STANDARD_REASONING_FLOOR else requested


def run_prompt(prompt, system=None, model=None, timeout=TIMEOUT_S, reasoning=None, max_tokens=None,
               *, agentic=False, cwd=None):
    """Run one authenticated headless Grok CLI completion and return the subscription result contract.

    ``max_tokens`` is accepted for the shared lane-executor contract, but Grok ``-p`` exposes no output-cap flag.
    """
    from ... import execution_journal
    durable_id = execution_journal.current_durable_execution_id()
    process_runner = (execution_journal.run_durable_delegate_subprocess if durable_id else subprocess.run)
    executable = _bin()
    if agentic and cwd is None:
        return {"error": "agentic Grok execution requires an explicit workspace"}
    if not executable:
        return {"error": "grok CLI not found"}
    full_prompt = f"{system.strip()}\n\n{prompt}" if system else prompt
    model_alias = _grok_model_alias(model)
    if model and not model_alias:
        return {"error": "requested model is not declared by the Grok CLI cache"}
    command = [executable, "-p", full_prompt]
    if model_alias:
        command += ["-m", model_alias]
    command += [GROK_REASONING_FLAG, _grok_effort(reasoning)]   # _grok_effort always yields an effort (low default)
    from ... import config
    environment = config.lane_exec_env(executable)
    started_at = time.time()
    neutral_directory = None
    try:
        if not agentic:
            neutral_directory = tempfile.mkdtemp(prefix=GROK_WORK_DIRECTORY_PREFIX)
        try:
            completed = process_runner(command, capture_output=True, text=True, timeout=timeout,
                                       env=environment, stdin=subprocess.DEVNULL,
                                       cwd=cwd if agentic else neutral_directory)
        except subprocess.TimeoutExpired:
            if durable_id:
                raise
            return {"error": f"grok lane timeout ({timeout}s)"}
        except Exception as error:
            if durable_id:
                raise
            return {"error": str(error)[:200]}
        if completed.returncode != 0:
            return {"error": (completed.stderr or completed.stdout or "grok exited non-zero").strip()[:200]}
        response_text = completed.stdout.strip()
        if not response_text:
            return {"error": "grok produced no response"}
        return {"text": response_text, "in_tok": len(full_prompt) // 4,
                "out_tok": len(response_text) // 4, "latency": time.time() - started_at, "error": None}
    finally:
        if neutral_directory:
            shutil.rmtree(neutral_directory, ignore_errors=True)
