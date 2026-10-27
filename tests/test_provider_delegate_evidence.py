"""Offline provider transport evidence survives malformed provider envelopes."""
import importlib
import io
import json
import os
import secrets
import sys
from unittest.mock import Mock

import pytest

from spendguard import config, execution_journal

PROVIDER_EVIDENCE_MODULE_NAMES = ("subscription_exec", "antigravity_exec", "kimi_exec")
PROVIDER_EVIDENCE_REPEAT_COUNT = 20000


@pytest.mark.parametrize("provider_module_name", PROVIDER_EVIDENCE_MODULE_NAMES)
def test_provider_malformed_envelope_preserves_whole_transport(provider_module_name, tmp_path, monkeypatch):
    monkeypatch.setenv("SPENDGUARD_EXECUTION_STORE", str(tmp_path / "records"))
    provider_module = importlib.import_module("spendguard." + provider_module_name)
    provider_stdout = "unparseable-envelope-" * PROVIDER_EVIDENCE_REPEAT_COUNT
    provider_stderr = "diagnostic-" * PROVIDER_EVIDENCE_REPEAT_COUNT
    tmp_path.mkdir(parents=True, exist_ok=True)
    executable = tmp_path / "offline-provider"
    executable.write_text(
        "#!" + sys.executable + "\nimport sys\n"
        + "sys.stdout.write(" + repr(provider_stdout) + ")\n"
        + "sys.stderr.write(" + repr(provider_stderr) + ")\n"
    )
    executable.chmod(0o700)
    monkeypatch.setattr(provider_module, "_bin", lambda: str(executable))
    monkeypatch.setattr(config, "lane_exec_env", lambda unused: os.environ.copy())
    if provider_module_name == "kimi_exec":
        monkeypatch.setattr(provider_module, "_daemon_enabled", lambda: False)
    execution_id = execution_journal.begin_durable_execution()
    try:
        with execution_journal.durable_execution_scope(execution_id):
            result = provider_module.run_prompt("offline fixture")
        assert result["error"]
        record = execution_journal.recover_durable_execution(execution_id)
        assert record["stdout"] == provider_stdout
        assert record["stderr"] == provider_stderr
        assert record["exit_code"] == 0
    finally:
        execution_journal.terminalize_durable_execution(execution_id, "failure")


def test_kimi_daemon_envelope_is_preserved_before_interpretation(tmp_path, monkeypatch):
    from spendguard import kimi_daemon, kimi_exec
    monkeypatch.setenv("SPENDGUARD_EXECUTION_STORE", str(tmp_path / "records"))
    monkeypatch.setattr(kimi_exec, "_bin", lambda: sys.executable)
    monkeypatch.setattr(kimi_exec, "_daemon_enabled", lambda: True)
    monkeypatch.setattr(kimi_exec, "_warm_model_eligible", lambda unused: True)
    warm_envelope = {"text": "whole answer" * PROVIDER_EVIDENCE_REPEAT_COUNT, "error": None}
    monkeypatch.setattr(kimi_daemon, "run_warm", lambda *args, **kwargs: warm_envelope)
    execution_id = execution_journal.begin_durable_execution()
    try:
        with execution_journal.durable_execution_scope(execution_id):
            assert kimi_exec.run_prompt("offline fixture")["text"] == warm_envelope["text"]
        record = execution_journal.recover_durable_execution(execution_id)
        assert json.loads(record["stdout"]) == warm_envelope
    finally:
        execution_journal.terminalize_durable_execution(execution_id, "success")


def test_zai_invalid_json_keeps_whole_http_response(tmp_path, monkeypatch):
    from spendguard import zai_exec
    monkeypatch.setenv("SPENDGUARD_EXECUTION_STORE", str(tmp_path / "records"))
    zai_offline_key = secrets.token_urlsafe()
    monkeypatch.setattr(zai_exec, "_key", lambda: zai_offline_key)
    zai_wire_payload = "invalid-json-" * PROVIDER_EVIDENCE_REPEAT_COUNT
    monkeypatch.setattr(zai_exec.urllib.request, "urlopen",
                        lambda *args, **kwargs: io.BytesIO(zai_wire_payload.encode()))
    execution_id = execution_journal.begin_durable_execution()
    try:
        with execution_journal.durable_execution_scope(execution_id):
            assert zai_exec.run_prompt("offline fixture", max_tokens=PROVIDER_EVIDENCE_REPEAT_COUNT)["error"]
        assert execution_journal.recover_durable_execution(execution_id)["stdout"] == zai_wire_payload
    finally:
        execution_journal.terminalize_durable_execution(execution_id, "failure")


WARM_CRASH_REPRO_TIMEOUT_S = 5
WARM_CRASH_REPRO_EXIT_CODE = 17
WARM_CRASH_SERVER_SOURCE = """import json, os, sys
with open(sys.argv[1]) as fixture_stream:
    chunk, diagnostic, exit_code = json.load(fixture_stream)
for request_line in sys.stdin:
    request = json.loads(request_line)
    method = request.get("method")
    if method == "initialize":
        payload = {}
    elif method == "session/new":
        payload = {"sessionId": "offline-session"}
    elif method == "session/prompt":
        print(json.dumps({"jsonrpc": "2.0", "method": "session/update", "params": {
            "sessionId": "offline-session", "update": {"sessionUpdate": "agent_message_chunk",
            "content": {"type": "text", "text": chunk}}}}), flush=True)
        sys.stderr.write(diagnostic)
        sys.stderr.flush()
        os._exit(int(exit_code))
    else:
        continue
    print(json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": payload}), flush=True)
"""


def test_kimi_warm_crash_preserves_streamed_chunks_and_stderr(tmp_path, monkeypatch):
    from spendguard import kimi_daemon
    monkeypatch.setenv("SPENDGUARD_EXECUTION_STORE", str(tmp_path / "records"))
    monkeypatch.setattr(config, "lane_exec_env", lambda unused: os.environ.copy())
    warm_fixture_path = tmp_path / "offline-warm-server.py"
    tmp_path.mkdir(parents=True, exist_ok=True)
    warm_fixture_path.write_text(WARM_CRASH_SERVER_SOURCE)
    warm_partial_text = "partial-completed-work-" * PROVIDER_EVIDENCE_REPEAT_COUNT
    warm_partial_diagnostic = "full-warm-diagnostic-" * PROVIDER_EVIDENCE_REPEAT_COUNT
    warm_fixture_config = tmp_path / "warm-fixture.json"
    warm_fixture_config.write_text(json.dumps([
        warm_partial_text, warm_partial_diagnostic, WARM_CRASH_REPRO_EXIT_CODE]))
    warm_daemon = kimi_daemon._KimiAcpDaemon()
    monkeypatch.setattr(warm_daemon, "_spawn_cmd", lambda: [
        sys.executable, str(warm_fixture_path), str(warm_fixture_config)])
    execution_id = execution_journal.begin_durable_execution()
    try:
        with execution_journal.durable_execution_scope(execution_id):
            assert warm_daemon.run_warm("offline fixture", timeout=WARM_CRASH_REPRO_TIMEOUT_S)["error"]
        warm_recovery = execution_journal.recover_durable_execution(execution_id)
        assert warm_partial_text in warm_recovery["stdout"]
        assert warm_partial_diagnostic in warm_recovery["stderr"]
        assert warm_recovery["stderr_attribution"] == "shared_daemon"
        assert warm_recovery["exit_code"] == WARM_CRASH_REPRO_EXIT_CODE
        warm_daemon.shutdown()
        monkeypatch.setattr(execution_journal, "append_durable_execution_evidence", Mock(
            side_effect=execution_journal.DurableExecutionPersistenceError("offline disk failure")))
        with execution_journal.durable_execution_scope(execution_id):
            with pytest.raises(execution_journal.DurableExecutionPersistenceError):
                warm_daemon.run_warm("offline fixture", timeout=WARM_CRASH_REPRO_TIMEOUT_S)
    finally:
        warm_daemon.shutdown()
        execution_journal.terminalize_durable_execution(execution_id, "failure")


if __name__ == "__main__":
    from pathlib import Path
    import tempfile
    with tempfile.TemporaryDirectory() as provider_repro_directory:
        for provider_repro_name in PROVIDER_EVIDENCE_MODULE_NAMES:
            with pytest.MonkeyPatch.context() as provider_repro_patch:
                test_provider_malformed_envelope_preserves_whole_transport(
                    provider_repro_name, Path(provider_repro_directory) / provider_repro_name,
                    provider_repro_patch)
        with pytest.MonkeyPatch.context() as provider_warm_patch:
            test_kimi_daemon_envelope_is_preserved_before_interpretation(
                Path(provider_repro_directory) / "warm", provider_warm_patch)
        with pytest.MonkeyPatch.context() as provider_http_patch:
            test_zai_invalid_json_keeps_whole_http_response(
                Path(provider_repro_directory) / "http", provider_http_patch)
        with pytest.MonkeyPatch.context() as provider_crash_patch:
            test_kimi_warm_crash_preserves_streamed_chunks_and_stderr(
                Path(provider_repro_directory) / "warm-crash", provider_crash_patch)
    print("provider delegated evidence: all checks passed")
