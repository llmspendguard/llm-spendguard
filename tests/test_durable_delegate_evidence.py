"""Offline regressions for recoverable output and dead-owner terminalization."""
import json
import contextlib
import os
import select
import subprocess
import sys
import time
import tempfile
from pathlib import Path

import pytest

from spendguard import codex_exec
try:
    from spendguard import execution_journal
except ImportError:
    execution_journal = None

DURABLE_REPRO_PAYLOAD_REPETITIONS = 200000
DURABLE_REPRO_WATCH_BOUND_S = 10
DURABLE_REPRO_POLL_S = 0.02
DURABLE_REPRO_CHILD_WAIT_S = 30
DURABLE_REPRO_LIFECYCLE_TARGET = "lifecycle"


def test_envelope_failure_after_output_preserves_complete_delegate_evidence(tmp_path, monkeypatch):
    if execution_journal is not None:
        monkeypatch.setattr(execution_journal, "durable_execution_store_root", lambda: tmp_path)
    monkeypatch.setattr(codex_exec, "_bin", lambda: sys.executable)
    monkeypatch.setattr(codex_exec.config if hasattr(codex_exec, "config") else
                        __import__("spendguard.config", fromlist=["config"]),
                        "lane_exec_env", lambda executable: os.environ.copy())
    monkeypatch.setattr(codex_exec, "_codex_delegate_read_only_tool_approval_arguments", lambda: [])
    whole_result = "recoverable-result-" * DURABLE_REPRO_PAYLOAD_REPETITIONS
    whole_error = "recoverable-stderr-" * DURABLE_REPRO_PAYLOAD_REPETITIONS

    def emit_repro_delegate_output(command, timeout, environment, cwd):
        path = command[command.index("--output-last-message") + 1]
        # Feed complete large evidence over stdin rather than exceeding OS argv capacity.
        source = ("import json,pathlib,sys; data=json.load(sys.stdin); "
                  "pathlib.Path(sys.argv[1]).write_text(data['result']); "
                  "sys.stdout.write(data['result']); sys.stderr.write(data['stderr'])")
        runner = execution_journal.run_durable_delegate_subprocess if execution_journal else subprocess.run
        return runner(
            [sys.executable, "-c", source, path], timeout=timeout, env=environment, cwd=cwd,
            input=json.dumps({"result": whole_result, "stderr": whole_error}), text=True, capture_output=True)

    def fail_repro_result_envelope(events):
        raise ValueError("forced envelope failure after child output")

    monkeypatch.setattr(codex_exec, "_run_cold_codex_with_reaper", emit_repro_delegate_output)
    monkeypatch.setattr(codex_exec, "_usage_from_events", fail_repro_result_envelope)
    dispatch_id = execution_journal.begin_durable_execution() if execution_journal else None
    scope = execution_journal.durable_execution_scope(dispatch_id) if execution_journal else contextlib.nullcontext()
    with scope:
        with pytest.raises(ValueError, match="forced envelope failure"):
            codex_exec._run_agentic_codex_exec("offline reproduction", cwd=tmp_path)
    assert execution_journal is not None, "completed output has no spendguard recovery record or recover path"
    execution_journal.terminalize_durable_execution(dispatch_id, "failure")
    recovered = execution_journal.recover_durable_execution(dispatch_id)
    assert recovered["result"] == whole_result
    assert recovered["stdout"] == whole_result
    assert recovered["stderr"] == whole_error
    assert recovered["exit_code"] == 0


def test_killed_delegate_owner_gets_one_terminal_and_normal_completion_is_once(tmp_path, monkeypatch):
    if execution_journal is None:
        source = ("import sys,time; from spendguard import delegate_router; "
                  "delegate_router._append_codex_agentic_event('start',sys.argv[1],plan='codex',model=None,cwd=sys.argv[2]); "
                  "print(delegate_router.codex_agentic_events_path(),flush=True); time.sleep(float(sys.argv[3]))")
        environment = os.environ.copy()
        environment["SPENDGUARD_HOME"] = os.fspath(tmp_path)
        baseline_child = subprocess.Popen([sys.executable, "-c", source, tmp_path.name, os.fspath(tmp_path),
                                            str(DURABLE_REPRO_CHILD_WAIT_S)], stdout=subprocess.PIPE,
                                           text=True, env=environment)
        try:
            ready, _, _ = select.select([baseline_child.stdout], [], [], DURABLE_REPRO_WATCH_BOUND_S)
            assert ready, "baseline child did not report its start before watchdog bound"
            path = Path(baseline_child.stdout.readline().strip())
            baseline_child.kill()
            baseline_child.wait(timeout=DURABLE_REPRO_WATCH_BOUND_S)
            rows = [json.loads(line) for line in path.read_text().splitlines()]
            assert sum(row["event"] == "start" for row in rows) == 1
            assert sum(row["event"] == "end" for row in rows) == 1, "dead pid left an orphan start"
        finally:
            baseline_child.stdout.close()
            if baseline_child.poll() is None:
                baseline_child.kill()
                baseline_child.wait(timeout=DURABLE_REPRO_WATCH_BOUND_S)
        return
    monkeypatch.setattr(execution_journal, "durable_execution_store_root", lambda: tmp_path)
    monkeypatch.setenv("SPENDGUARD_EXECUTION_WATCH_INTERVAL_S", str(DURABLE_REPRO_POLL_S))
    child = subprocess.Popen([sys.executable, "-c", "import sys,time; time.sleep(float(sys.argv[1]))",
                              str(DURABLE_REPRO_CHILD_WAIT_S)])
    try:
        dispatch_id = execution_journal.begin_durable_execution({"owner_pid": child.pid})
        child.kill()
        child.wait(timeout=DURABLE_REPRO_WATCH_BOUND_S)
        event_path = tmp_path / dispatch_id / execution_journal.EXECUTION_EVENTS_FILENAME
        deadline = time.monotonic() + DURABLE_REPRO_WATCH_BOUND_S
        while time.monotonic() < deadline:
            events = [json.loads(line) for line in event_path.read_text().splitlines()]
            if any(row["phase"] == "end" for row in events):
                break
            time.sleep(DURABLE_REPRO_POLL_S)
        assert sum(row["phase"] == "start" for row in events) == 1
        assert sum(row["phase"] == "end" for row in events) == 1
        assert execution_journal.recover_durable_execution(dispatch_id)["state"] == "lost"
        execution_journal.terminalize_durable_execution(dispatch_id, "lost")
        normal_id = execution_journal.begin_durable_execution()
        execution_journal.terminalize_durable_execution(normal_id, "success", exit_code=0, result="first")
        execution_journal.terminalize_durable_execution(normal_id, "failure", exit_code=1, result="replacement")
        # Simulate an end appended before a crash prevented the state replacement.
        normal_directory = tmp_path / normal_id
        interrupted_state = json.loads((normal_directory / execution_journal.EXECUTION_STATE_FILENAME).read_text())
        interrupted_state.update(state="running", exit_code=None)
        execution_journal._write_durable_execution_state(normal_directory, interrupted_state)
        execution_journal.terminalize_durable_execution(normal_id, "failure", exit_code=2, result="crash replacement")
        recovered_normal = execution_journal.recover_durable_execution(normal_id)
        assert recovered_normal["result"] == "first"
        assert recovered_normal["exit_code"] == 0
        normal_events = (tmp_path / normal_id / execution_journal.EXECUTION_EVENTS_FILENAME).read_text().splitlines()
        assert sum(json.loads(line)["phase"] == "end" for line in normal_events) == 1
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=DURABLE_REPRO_WATCH_BOUND_S)


if __name__ == "__main__":
    if sys.argv[-1] != DURABLE_REPRO_LIFECYCLE_TARGET:
        with tempfile.TemporaryDirectory() as evidence_directory, pytest.MonkeyPatch.context() as patches:
            test_envelope_failure_after_output_preserves_complete_delegate_evidence(Path(evidence_directory), patches)
    with tempfile.TemporaryDirectory() as lifecycle_directory, pytest.MonkeyPatch.context() as patches:
        test_killed_delegate_owner_gets_one_terminal_and_normal_completion_is_once(Path(lifecycle_directory), patches)
    print("OK test_durable_delegate_evidence")
