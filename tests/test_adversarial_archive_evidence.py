"""Offline adversarial probes for delegated transport evidence."""
import socket
import threading
import tempfile
import http.client
import io
import secrets
from pathlib import Path
from types import SimpleNamespace

import pytest

from spendguard import codex_daemon_broker, codex_exec, execution_journal
from spendguard.warm_stdio_daemon import _WarmStdioJsonRpcDaemon

ARCHIVE_REPRO_PARTIAL_OUTPUT = b'partial provider work before transport failure\n'
ARCHIVE_REPRO_WAIT_S = 5
ARCHIVE_REPRO_CHUNK_HEADER = b'HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n'


def test_zai_mid_chunk_disconnect_preserves_partial_response(tmp_path, monkeypatch):
    from spendguard import zai_exec
    monkeypatch.setenv("SPENDGUARD_EXECUTION_STORE", str(tmp_path))
    archive_http_key = secrets.token_urlsafe()
    monkeypatch.setattr(zai_exec, "_key", lambda: archive_http_key)
    archive_http_wire = (ARCHIVE_REPRO_CHUNK_HEADER
                         + format(len(ARCHIVE_REPRO_PARTIAL_OUTPUT), "x").encode()
                         + b'\r\n' + ARCHIVE_REPRO_PARTIAL_OUTPUT + b'\r\n')

    def archive_repro_http_makefile(*args, **kwargs):
        return io.BytesIO(archive_http_wire)

    def archive_repro_http_open(*args, **kwargs):
        response = http.client.HTTPResponse(SimpleNamespace(makefile=archive_repro_http_makefile))
        response.begin()
        return response

    monkeypatch.setattr(zai_exec.urllib.request, "urlopen", archive_repro_http_open)
    execution_id = execution_journal.begin_durable_execution()
    try:
        with execution_journal.durable_execution_scope(execution_id):
            response = zai_exec.run_prompt("offline fixture")
        assert response["error"]
        record = execution_journal.recover_durable_execution(execution_id)
        assert record["stdout"].encode() == ARCHIVE_REPRO_PARTIAL_OUTPUT, repr(record["stdout"])
    finally:
        execution_journal.terminalize_durable_execution(execution_id, "failure")


def test_remote_codex_broker_keeps_partial_transport_evidence(tmp_path, monkeypatch):
    monkeypatch.setenv("SPENDGUARD_EXECUTION_STORE", str(tmp_path))
    transport = _WarmStdioJsonRpcDaemon()

    def archive_repro_remote_warm_call(**kwargs):
        capture_id = transport._enter_warm_evidence_scope()
        try:
            transport._capture_warm_transport_bytes(stdout=ARCHIVE_REPRO_PARTIAL_OUTPUT)
            raise ConnectionError("offline server died after producing work")
        finally:
            transport._leave_warm_evidence_scope(capture_id)

    broker = codex_daemon_broker._CodexDaemonBroker(SimpleNamespace(
        CALL_TIMEOUT_S=ARCHIVE_REPRO_WAIT_S, run_warm=archive_repro_remote_warm_call))
    client, server = socket.socketpair()
    broker._handler_slots.acquire()
    execution_id = execution_journal.begin_durable_execution()
    thread = threading.Thread(target=broker._serve_one, args=(server,))
    try:
        with execution_journal.durable_execution_scope(execution_id):
            thread.start()
            codex_daemon_broker._send_frame(client, {
                "version": codex_daemon_broker._PROTOCOL_VERSION,
                "method": "run_warm", "kwargs": {"prompt": "offline fixture"}})
            response = codex_daemon_broker._receive_frame(client)
        assert response["result"]["error"]
        thread.join(ARCHIVE_REPRO_WAIT_S)
        record = execution_journal.recover_durable_execution(execution_id)
        assert record["stdout"].encode() == ARCHIVE_REPRO_PARTIAL_OUTPUT, repr(record["stdout"])
    finally:
        client.close()
        thread.join(ARCHIVE_REPRO_WAIT_S)
        execution_journal.terminalize_durable_execution(execution_id, "failure")


def test_codex_warm_persistence_failure_surfaces(tmp_path, monkeypatch):
    from spendguard import codex_daemon
    monkeypatch.setenv("SPENDGUARD_EXECUTION_STORE", str(tmp_path))
    monkeypatch.setattr(codex_exec, "_daemon_enabled", lambda: True)

    def archive_repro_failed_warm_call(*args, **kwargs):
        raise execution_journal.DurableExecutionPersistenceError("offline disk failure")

    monkeypatch.setattr(codex_daemon, "run_warm", archive_repro_failed_warm_call)
    execution_id = execution_journal.begin_durable_execution()
    try:
        with execution_journal.durable_execution_scope(execution_id):
            with pytest.raises(execution_journal.DurableExecutionPersistenceError):
                codex_exec._run_prompt_in_cwd("offline fixture", cwd=tmp_path)
    finally:
        execution_journal.terminalize_durable_execution(execution_id, "failure")


def test_broker_reader_retains_client_execution_store(tmp_path, monkeypatch):
    monkeypatch.setenv("SPENDGUARD_EXECUTION_STORE", str(tmp_path))
    execution_id = execution_journal.begin_durable_execution()
    transport = _WarmStdioJsonRpcDaemon()
    capture_errors = []
    try:
        with execution_journal.durable_execution_scope(execution_id, root=tmp_path):
            capture_id = transport._enter_warm_evidence_scope()
        with tempfile.TemporaryDirectory() as unrelated_store:
            monkeypatch.setenv("SPENDGUARD_EXECUTION_STORE", unrelated_store)

            def archive_repro_reader_without_dispatch_context():
                try:
                    transport._capture_warm_transport_bytes(stdout=ARCHIVE_REPRO_PARTIAL_OUTPUT)
                except BaseException as reader_error:
                    capture_errors.append(reader_error)

            reader = threading.Thread(target=archive_repro_reader_without_dispatch_context)
            reader.start()
            reader.join(ARCHIVE_REPRO_WAIT_S)
            assert not reader.is_alive()
            assert not capture_errors
            transport._leave_warm_evidence_scope(capture_id)
        record = execution_journal.recover_durable_execution(execution_id, root=tmp_path)
        assert record["stdout"].encode() == ARCHIVE_REPRO_PARTIAL_OUTPUT
    finally:
        execution_journal.terminalize_durable_execution(execution_id, "failure", root=tmp_path)


def test_broker_persistence_failure_roundtrip_keeps_exception_type(tmp_path, monkeypatch):
    def archive_repro_broker_disk_failure(**kwargs):
        raise execution_journal.DurableExecutionPersistenceError(ARCHIVE_REPRO_PARTIAL_OUTPUT.decode())

    broker = codex_daemon_broker._CodexDaemonBroker(SimpleNamespace(
        CALL_TIMEOUT_S=ARCHIVE_REPRO_WAIT_S, run_warm=archive_repro_broker_disk_failure))
    client, server = socket.socketpair()
    client_facade = SimpleNamespace(connect=lambda path: None, settimeout=client.settimeout,
                                    sendall=client.sendall, recv=client.recv, close=client.close)
    monkeypatch.setattr(codex_daemon_broker.socket, "socket", lambda *args: client_facade)
    broker._handler_slots.acquire()
    server_thread = threading.Thread(target=broker._serve_one, args=(server,))
    server_thread.start()
    try:
        with pytest.raises(execution_journal.DurableExecutionPersistenceError) as surfaced:
            broker._call_owner({"prompt": ARCHIVE_REPRO_PARTIAL_OUTPUT.decode()})
        assert str(surfaced.value) == ARCHIVE_REPRO_PARTIAL_OUTPUT.decode()
    finally:
        client.close()
        server_thread.join(ARCHIVE_REPRO_WAIT_S)
        assert not server_thread.is_alive()


if __name__ == "__main__":
    archive_repro_failures = []
    for archive_repro_test in (test_remote_codex_broker_keeps_partial_transport_evidence,
                               test_codex_warm_persistence_failure_surfaces,
                               test_zai_mid_chunk_disconnect_preserves_partial_response,
                               test_broker_reader_retains_client_execution_store,
                               test_broker_persistence_failure_roundtrip_keeps_exception_type):
        with tempfile.TemporaryDirectory() as archive_repro_directory, pytest.MonkeyPatch.context() as archive_patch:
            try:
                archive_repro_test(Path(archive_repro_directory), archive_patch)
            except BaseException as archive_error:
                archive_repro_failures.append(archive_repro_test.__name__)
                print(f"FAIL {archive_repro_test.__name__}: {type(archive_error).__name__}: {archive_error}")
    assert not archive_repro_failures, archive_repro_failures
