"""Offline fault injection: a missing execution result can never look successful."""
import contextlib
import io
import json
import subprocess
import sys
import tempfile
from unittest.mock import patch

from spendguard import codex_daemon, codex_exec, delegate_router

DELEGATE_GUARD_TASK = "execution result fixture"
DELEGATE_GUARD_PLAN = "codex"
DELEGATE_GUARD_MODEL = "execution-fixture-model"
DELEGATE_GUARD_CLASSIFICATION = {
    "kind": "agentic", "provider": DELEGATE_GUARD_PLAN,
    "self_contained": True, "why": "isolated execution fixture",
}
DELEGATE_GUARD_ESTIMATE = {
    "plan": DELEGATE_GUARD_PLAN, "model": DELEGATE_GUARD_MODEL,
    "real_api_usd": 0.0, "est_value_usd": 0.0,
    "input_tokens": 0, "output_tokens": 0,
}
DELEGATE_GUARD_REFUSAL = "fixture admission refused before process startup"
DELEGATE_GUARD_CONSOLE_SCRIPT = """
import sys
import spendguard
spendguard.require()
import json
from unittest.mock import patch
from spendguard import cli, delegate_router
def execute_guard_console_job(task, **kw):
    return delegate_router.delegate_task(task, execute=True, **{key: value for key, value in kw.items() if key != 'detached'})
fixture = json.loads(sys.argv[1])
with patch.object(delegate_router, 'classify_task', return_value=fixture['classification']), \
        patch.object(delegate_router, 'delegation_lanes_ready', return_value=fixture['ready']), \
        patch.object(delegate_router, '_estimate_route', return_value=fixture['estimate']), \
        patch.object(delegate_router.config, 'intent_cap', return_value=None), \
        patch.object(delegate_router, '_worktree_state', return_value=(None, set())), \
        patch.object(delegate_router, '_worktree_changed_files', return_value=None), \
        patch.object(delegate_router, '_route_agentic', return_value=None), \
        patch.object(delegate_router, 'start_delegate_job', side_effect=execute_guard_console_job):
    sys.exit(cli.main(sys.argv[2:]))
"""
DELEGATE_GUARD_CONSOLE_ERROR_STATUS = 2
DELEGATE_GUARD_PROVIDER_HTTP_STATUS = 429
DELEGATE_GUARD_DIAGNOSTIC_REPETITIONS = 32
DELEGATE_GUARD_PROVIDER_ERROR = {
    "message": "full protocol error diagnostic " * DELEGATE_GUARD_DIAGNOSTIC_REPETITIONS,
    "codexErrorInfo": {"responseTooManyFailedAttempts": {
        "httpStatusCode": DELEGATE_GUARD_PROVIDER_HTTP_STATUS}},
}
DELEGATE_GUARD_FAULTS = (
    None,
    {},
    {"error": None},
    {"text": ""},
    {"text": " \n"},
    {"execution_started": True, "text": ""},
    {"text": {"invalid": "reply"}},
    {"text": "fabricated result without any agent process"},
    {"text": "untrusted partial reply", "error": DELEGATE_GUARD_REFUSAL},
)

with tempfile.TemporaryDirectory() as workspace, \
        patch.object(delegate_router, "classify_task", return_value=DELEGATE_GUARD_CLASSIFICATION), \
        patch.object(delegate_router, "delegation_lanes_ready", return_value={"ready": [DELEGATE_GUARD_PLAN]}), \
        patch.object(delegate_router, "_estimate_route", return_value=DELEGATE_GUARD_ESTIMATE), \
        patch.object(delegate_router.config, "intent_cap", return_value=None), \
        patch.object(delegate_router, "_worktree_state", return_value=(None, set())), \
        patch.object(delegate_router, "_worktree_changed_files", return_value=None), \
        patch.object(delegate_router, "_route_agentic") as executor, \
        patch.object(delegate_router, "start_delegate_job", side_effect=lambda task, **kw: delegate_router.delegate_task(task, execute=True, **{key: value for key, value in kw.items() if key != "detached"})):
    for fault in DELEGATE_GUARD_FAULTS:
        executor.return_value = fault
        result = delegate_router.delegate_task(DELEGATE_GUARD_TASK, execute=True, cwd=workspace)
        assert result["status"] == "error", result
        assert result.get("reason") and result.get("why"), result
        output = io.StringIO()
        with contextlib.redirect_stderr(output):
            exit_status = delegate_router.delegate_cli([
                DELEGATE_GUARD_TASK, "--provider", DELEGATE_GUARD_PLAN, "--cwd", workspace, "--yes"])
        assert exit_status != 0, output.getvalue()
        assert result["reason"] in output.getvalue(), output.getvalue()
        assert "delegate error: None" not in output.getvalue(), output.getvalue()
        if isinstance(fault, dict) and fault.get("error"):
            assert DELEGATE_GUARD_REFUSAL in output.getvalue(), output.getvalue()

console = subprocess.run(
    [sys.executable, "-c", DELEGATE_GUARD_CONSOLE_SCRIPT,
     json.dumps({"classification": DELEGATE_GUARD_CLASSIFICATION,
                 "ready": {"ready": [DELEGATE_GUARD_PLAN]}, "estimate": DELEGATE_GUARD_ESTIMATE}),
     "delegate", DELEGATE_GUARD_TASK, "--yes"],
    capture_output=True, text=True, check=False)
assert console.returncode == DELEGATE_GUARD_CONSOLE_ERROR_STATUS, console
assert delegate_router.DELEGATE_EXECUTION_INVALID_RESULT in console.stderr, console
assert "delegate error: None" not in console.stderr, console

provider_refusal = codex_daemon._codex_pool_error_result(DELEGATE_GUARD_PROVIDER_ERROR, None)
assert provider_refusal["error"] == str(DELEGATE_GUARD_PROVIDER_ERROR), provider_refusal
with patch.object(codex_exec, "_daemon_enabled", return_value=True), \
        patch.object(codex_daemon, "run_warm", return_value=provider_refusal):
    transport_refusal = codex_exec._run_prompt_in_cwd(DELEGATE_GUARD_TASK)
assert transport_refusal["status_code"] == DELEGATE_GUARD_PROVIDER_HTTP_STATUS, transport_refusal
assert transport_refusal["error"] == str(DELEGATE_GUARD_PROVIDER_ERROR), transport_refusal

print("OK test_delegate_execution_result_guard")
