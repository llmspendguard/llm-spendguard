"""Offline Codex entrypoint: warm broker only, no cold process after refusal."""
import json
import os
import tempfile
from unittest.mock import patch

from spendguard import codex_daemon, codex_exec, config

CODEX_ENTRY_PROMPT = "entrypoint fixture"
CODEX_ENTRY_SYSTEM = "system fixture"
CODEX_ENTRY_REPLY = "broker reply"
CODEX_ENTRY_USAGE_INPUT = 850
CODEX_ENTRY_USAGE_OUTPUT = 97
CODEX_ENTRY_EXECUTABLE_MODE = 0o700
CODEX_ENTRY_FAILURES = (
    {"error": "broker unavailable"},
    {"error": "waiter limit reached"},
    {"error": "model rejected", "tool_error": True},
    {"text": ""},
    RuntimeError("warm admission exception"),
)

with patch.object(codex_exec.subprocess, "Popen") as spawn, \
        patch.object(codex_exec.subprocess, "run") as cold_run, \
        patch.object(codex_exec, "_daemon_enabled", return_value=True), \
        patch.object(codex_daemon, "run_warm", return_value={"text": CODEX_ENTRY_REPLY}) as warm:
    result = codex_exec.run_prompt(CODEX_ENTRY_PROMPT, system=CODEX_ENTRY_SYSTEM)
    assert result["text"] == CODEX_ENTRY_REPLY and result["error"] is None
    assert warm.call_args.args == (f"{CODEX_ENTRY_SYSTEM}\n\n{CODEX_ENTRY_PROMPT}",)
    assert warm.call_args.kwargs["sandbox"] == "read-only"
    assert not os.path.exists(warm.call_args.kwargs["cwd"])
    with tempfile.TemporaryDirectory() as trusted_cwd:
        codex_exec.run_prompt(CODEX_ENTRY_PROMPT, sandbox="workspace-write", cwd=trusted_cwd)
        assert warm.call_args.kwargs["sandbox"] == "workspace-write"
        assert warm.call_args.kwargs["cwd"] == trusted_cwd
    for failure in CODEX_ENTRY_FAILURES:
        warm.side_effect = failure if isinstance(failure, Exception) else None
        warm.return_value = failure
        for sandbox in ("read-only", "workspace-write"):
            result = codex_exec.run_prompt(CODEX_ENTRY_PROMPT, sandbox=sandbox, cwd=os.getcwd())
            assert result.get("error") and not result.get("text"), result
    warm.reset_mock()
    with patch.object(codex_exec, "_daemon_enabled", return_value=False):
        assert "refused" in codex_exec.run_prompt(CODEX_ENTRY_PROMPT)["error"]
        warm.assert_not_called()
    spawn.assert_not_called()
    cold_run.assert_not_called()

assert "unsupported" in codex_exec.run_prompt(CODEX_ENTRY_PROMPT, sandbox="invalid")["error"]
assert "explicit trusted cwd" in codex_exec.run_prompt(CODEX_ENTRY_PROMPT, sandbox="workspace-write")["error"]
events = [
    {"info": {"input_tokens": CODEX_ENTRY_USAGE_INPUT, "output_tokens": CODEX_ENTRY_USAGE_OUTPUT}},
    {"info": {"input_tokens": CODEX_ENTRY_USAGE_INPUT, "output_tokens": 0}},
]
assert codex_exec._usage_from_events("\n".join(map(json.dumps, events))) == (
    CODEX_ENTRY_USAGE_INPUT, CODEX_ENTRY_USAGE_OUTPUT)
assert codex_exec._usage_from_events("not json") == (0, 0)
with tempfile.NamedTemporaryFile() as pinned, patch.dict(os.environ, {"SPENDGUARD_CODEX_BIN": pinned.name}):
    os.chmod(pinned.name, CODEX_ENTRY_EXECUTABLE_MODE)
    assert config.resolve_cli("codex", "SPENDGUARD_CODEX_BIN") == pinned.name
print("OK test_codex_exec")
