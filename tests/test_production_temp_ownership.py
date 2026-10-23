"""Offline lifecycle proofs for production temporary batch and agent-exec artifacts."""
import json
import pathlib
import subprocess
import tempfile
from types import SimpleNamespace
from unittest.mock import patch

from spendguard import antigravity_exec, adapters, models, submit


BATCH_REQUEST_PREFIX = submit.CHAT_BATCH_REQUEST_DIRECTORY_PREFIX
ANTIGRAVITY_WORKDIR_PREFIX = antigravity_exec.ANTIGRAVITY_WORK_DIRECTORY_PREFIX
OFFLINE_ANTIGRAVITY_EXECUTABLE = "offline-antigravity"
OFFLINE_BATCH_MODEL = "offline-model"
OFFLINE_BATCH_CUSTOM_ID = "owned"
OFFLINE_BATCH_CONTENT = "offline"
OFFLINE_PROMPT = "offline prompt"
EXPECTED_BATCH_REQUEST_COUNT = 1
BATCH_OUTPUT_BUDGET = 1024


def matching_temp_entries(prefix):
    return {entry.name for entry in pathlib.Path(tempfile.gettempdir()).iterdir()
            if entry.name.startswith(prefix)}


def test_chat_batch_temp_closes_on_success_and_exception(tmp_path, monkeypatch):
    monkeypatch.setattr(adapters, "provider_for", lambda model: "openai")
    monkeypatch.setattr(adapters, "output_budget", lambda model: BATCH_OUTPUT_BUDGET)
    monkeypatch.setattr(models, "resolve_effort", lambda model, reasoning: None)
    tasks_path = tmp_path / "tasks.jsonl"
    tasks_path.write_text(json.dumps({"custom_id": OFFLINE_BATCH_CUSTOM_ID,
                                      "content": OFFLINE_BATCH_CONTENT}) + "\n")
    before = matching_temp_entries(BATCH_REQUEST_PREFIX)
    built_path, request_count = submit.build_chat_batch_jsonl(tasks_path, OFFLINE_BATCH_MODEL)
    assert request_count == EXPECTED_BATCH_REQUEST_COUNT
    assert pathlib.Path(built_path).is_file()
    submit.release_chat_batch_jsonl(built_path)
    assert matching_temp_entries(BATCH_REQUEST_PREFIX) == before

    tasks_path.write_text("not-json\n")
    try:
        submit.build_chat_batch_jsonl(tasks_path, OFFLINE_BATCH_MODEL)
    except ValueError:
        pass
    else:
        raise AssertionError("malformed task input did not exercise the exception cleanup path")
    assert matching_temp_entries(BATCH_REQUEST_PREFIX) == before


def test_antigravity_workdir_closes_on_success_and_exception(monkeypatch):
    monkeypatch.setattr(antigravity_exec, "_bin", lambda: OFFLINE_ANTIGRAVITY_EXECUTABLE)
    monkeypatch.setattr("spendguard.config.lane_exec_env", lambda executable: {})
    success_result = SimpleNamespace(
        returncode=0,
        stderr="",
        stdout=json.dumps({"status": "SUCCESS", "response": "offline", "usage": {}}),
    )
    before = matching_temp_entries(ANTIGRAVITY_WORKDIR_PREFIX)
    with patch.object(subprocess, "run", return_value=success_result):
        result = antigravity_exec.run_prompt(OFFLINE_PROMPT)
    assert result["error"] is None
    assert matching_temp_entries(ANTIGRAVITY_WORKDIR_PREFIX) == before

    with patch.object(subprocess, "run", side_effect=RuntimeError("offline failure")):
        result = antigravity_exec.run_prompt(OFFLINE_PROMPT)
    assert "offline failure" in result["error"]
    assert matching_temp_entries(ANTIGRAVITY_WORKDIR_PREFIX) == before


if __name__ == "__main__":
    from _pytest_fixture_bridge import run_pytest_style
    raise SystemExit(run_pytest_style(globals()))
