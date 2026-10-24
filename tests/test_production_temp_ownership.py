"""Offline lifecycle proofs for production temporary batch and agent-exec artifacts."""
import json
import pathlib
import sqlite3
import subprocess
import tempfile
from types import SimpleNamespace
from unittest.mock import patch

from spendguard import antigravity_exec, adapters, chat, codex_exec, kimi_exec, models, submit, subscription_exec


BATCH_REQUEST_PREFIX = submit.CHAT_BATCH_REQUEST_DIRECTORY_PREFIX
ANTIGRAVITY_WORKDIR_PREFIX = antigravity_exec.ANTIGRAVITY_WORK_DIRECTORY_PREFIX
CODEX_WORKDIR_PREFIX = codex_exec.CODEX_EXEC_WORK_DIRECTORY_PREFIX
KIMI_WORKDIR_PREFIX = kimi_exec.KIMI_WORK_DIRECTORY_PREFIX
SUBSCRIPTION_WORKDIR_PREFIX = subscription_exec.SUBSCRIPTION_WORK_DIRECTORY_PREFIX
CHAT_COOKIE_WORKDIR_PREFIX = chat.CHAT_COOKIE_COPY_DIRECTORY_PREFIX
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


def test_codex_workdir_closes_on_success_and_exception(monkeypatch):
    monkeypatch.setattr(codex_exec, "_run_prompt_in_cwd", lambda *args, **kwargs: {"error": None})
    before = matching_temp_entries(CODEX_WORKDIR_PREFIX)
    assert codex_exec.run_prompt(OFFLINE_PROMPT)["error"] is None
    assert matching_temp_entries(CODEX_WORKDIR_PREFIX) == before
    monkeypatch.setattr(codex_exec, "_run_prompt_in_cwd", lambda *args, **kwargs: (_ for _ in ()).throw(
        TimeoutError("offline timeout")))
    try:
        codex_exec.run_prompt(OFFLINE_PROMPT)
    except TimeoutError:
        pass
    assert matching_temp_entries(CODEX_WORKDIR_PREFIX) == before


def test_kimi_workdir_closes_on_success_and_timeout(monkeypatch):
    monkeypatch.setattr(kimi_exec, "_bin", lambda: OFFLINE_ANTIGRAVITY_EXECUTABLE)
    monkeypatch.setattr(kimi_exec, "_daemon_enabled", lambda: False)
    monkeypatch.setattr("spendguard.config.lane_exec_env", lambda executable: {})
    success = SimpleNamespace(returncode=0, stderr="", stdout='{"role":"assistant","content":"offline"}\n')
    before = matching_temp_entries(KIMI_WORKDIR_PREFIX)
    with patch.object(subprocess, "run", return_value=success):
        kimi_exec.run_prompt(OFFLINE_PROMPT)
    assert matching_temp_entries(KIMI_WORKDIR_PREFIX) == before
    with patch.object(subprocess, "run", side_effect=subprocess.TimeoutExpired("kimi", 1)):
        kimi_exec.run_prompt(OFFLINE_PROMPT)
    assert matching_temp_entries(KIMI_WORKDIR_PREFIX) == before


def test_subscription_workdir_closes_on_success_and_timeout(monkeypatch):
    monkeypatch.setattr(subscription_exec, "_bin", lambda: OFFLINE_ANTIGRAVITY_EXECUTABLE)
    monkeypatch.setattr("spendguard.config.lane_exec_env", lambda executable: {})
    success = SimpleNamespace(returncode=0, stderr="", stdout=json.dumps({"result": "offline", "usage": {}}))
    before = matching_temp_entries(SUBSCRIPTION_WORKDIR_PREFIX)
    with patch.object(subprocess, "run", return_value=success):
        subscription_exec.run_prompt(OFFLINE_PROMPT)
    assert matching_temp_entries(SUBSCRIPTION_WORKDIR_PREFIX) == before
    with patch.object(subprocess, "run", side_effect=subprocess.TimeoutExpired("subscription", 1)):
        subscription_exec.run_prompt(OFFLINE_PROMPT)
    assert matching_temp_entries(SUBSCRIPTION_WORKDIR_PREFIX) == before


def test_chat_cookie_copy_closes_on_success_and_exception(tmp_path, monkeypatch):
    cookie_database = tmp_path / "Cookies"
    connection = sqlite3.connect(cookie_database)
    connection.execute("create table cookies (host_key text, name text, encrypted_value blob)")
    connection.commit()
    connection.close()
    monkeypatch.setattr(chat, "_COOKIES", cookie_database)
    monkeypatch.setattr(chat, "_keychain_password", lambda: "offline")
    monkeypatch.setattr(chat, "_derive_key", lambda password: b"offline")
    before = matching_temp_entries(CHAT_COOKIE_WORKDIR_PREFIX)
    assert chat._decrypt_cookies() == {}
    assert matching_temp_entries(CHAT_COOKIE_WORKDIR_PREFIX) == before
    with patch.object(sqlite3, "connect", side_effect=RuntimeError("offline sqlite failure")):
        try:
            chat._decrypt_cookies()
        except RuntimeError:
            pass
    assert matching_temp_entries(CHAT_COOKIE_WORKDIR_PREFIX) == before

if __name__ == "__main__":
    from _pytest_fixture_bridge import run_pytest_style
    raise SystemExit(run_pytest_style(globals()))
