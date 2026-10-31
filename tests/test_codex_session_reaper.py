"""Offline completion-driven Codex retention contracts; never run a real CLI."""
import datetime
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
from types import SimpleNamespace
from unittest.mock import patch
import uuid

REAPER_TEST_HOME = tempfile.mkdtemp(prefix="sg-session-reaper-")
os.environ["SPENDGUARD_HOME"] = REAPER_TEST_HOME
os.environ.setdefault("SPENDGUARD_TEST_ISOLATED", "1")
os.environ.setdefault("SPENDGUARD_NO_AUTOINSTALL", "1")
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from spendguard import codex_daemon, codex_exec, config, delegate_jobs, delegate_router, warm_stdio_daemon  # noqa: E402

config.HOME = Path(REAPER_TEST_HOME)
REAPER_TEST_NOW = datetime.datetime.now(datetime.timezone.utc)
REAPER_TEST_SETTINGS = {"enabled": True, "success_grace_s": 30, "failure_grace_s": 60,
                        "max_per_pass": 20, "delete_timeout_s": 10}
REAPER_TEST_OLD_AGE_S = 90
REAPER_TEST_MIDDLE_AGE_S = 45
REAPER_TEST_NEW_AGE_S = 10
REAPER_TEST_LIMIT = 2
REAPER_TEST_BINARY = str(Path(REAPER_TEST_HOME) / "mock-codex")


def make_session_reaper_job(state="done", age=REAPER_TEST_OLD_AGE_S, thread=True):
    session_id = str(uuid.uuid4()) if thread else None
    job_id = delegate_jobs.create_delegate_job(
        kind="agentic", task_fingerprint=uuid.uuid4().hex, cwd=REAPER_TEST_HOME,
        provider="codex", intent="session-retention-test", dirty_before=set())
    if state != "running":
        delegate_jobs.finish_delegate_job(job_id, state=state,
                                          result_payload={"execution": {"thread": session_id}})
    with delegate_jobs._open_delegate_jobs_connection() as connection:
        stamp = (REAPER_TEST_NOW - datetime.timedelta(seconds=age)).isoformat()
        connection.execute("UPDATE delegate_jobs SET updated_ts=?,codex_thread_id=? WHERE job_id=?",
                           (stamp, session_id, job_id))
    return job_id, session_id


def read_session_reaper_row(job_id):
    return dict(delegate_jobs._delegate_job_row(job_id))


def run_session_reaper_fixture():
    return delegate_jobs.reap_terminal_codex_sessions(
        now=REAPER_TEST_NOW, executable=codex_exec._bin(),
        owner_identity_state=warm_stdio_daemon._warm_owner_identity_state,
        dead_identity=warm_stdio_daemon.WARM_OWNER_IDENTITY_DEAD)


def check_session_reaper_migration():
    with sqlite3.connect(":memory:") as connection:
        connection.execute("CREATE TABLE delegate_jobs(job_id TEXT PRIMARY KEY,created_ts TEXT,"
                           "updated_ts TEXT,state TEXT,kind TEXT,task_fingerprint TEXT,cwd TEXT,"
                           "provider TEXT,intent TEXT,dirty_before TEXT,result_payload TEXT,"
                           "terminal_reason TEXT,error TEXT)")
        connection.execute("INSERT INTO delegate_jobs(job_id,result_payload) VALUES(?,?)",
                           ("preserved-row", json.dumps({"whole": "payload"})))
        delegate_jobs._ensure_delegate_jobs_schema(connection)
        delegate_jobs._ensure_delegate_jobs_schema(connection)
        columns = {row[1] for row in connection.execute("PRAGMA table_info(delegate_jobs)")}
        assert {"codex_thread_id", "codex_session_reaped_at"} <= columns
        assert connection.execute("SELECT result_payload FROM delegate_jobs").fetchone()[0] == json.dumps({"whole": "payload"})
        indexed_columns = []
        for index in connection.execute("PRAGMA index_list(delegate_jobs)").fetchall():
            indexed_columns.extend(row[2] for row in connection.execute(f'PRAGMA index_info("{index[1]}")'))
        assert "codex_thread_id" in indexed_columns


def check_session_reaper_terminal_capture():
    for warm in (False, True):
        for failed in (False, True):
            job_id, _ = make_session_reaper_job(state="running", thread=False)
            session_id = str(uuid.uuid4())
            execution = {"thread": session_id, "text": None if failed else "complete",
                         "error": "fixture execution failure" if failed else None}
            assembled = {"status": "error" if failed else "executed", "execution": execution}
            with patch.object(delegate_router, "_route_agentic", return_value=execution), \
                    patch.object(delegate_router, "_assemble_delegate_execution_result", return_value=assembled):
                delegate_router._run_delegate_job_to_completion(
                    job_id, "codex", "fixture task", None, None, REAPER_TEST_HOME, set(),
                    result={}, classification={}, estimate={}, intent="session-retention-test", warm=warm)
            assert read_session_reaper_row(job_id)["codex_thread_id"] == session_id
            assert read_session_reaper_row(job_id)["state"] == ("failed" if failed else "done")
    direct_job, _ = make_session_reaper_job(state="running", thread=False)
    direct_thread = str(uuid.uuid4())
    delegate_jobs.finish_delegate_job(direct_job, state="failed", result_payload={"thread": direct_thread})
    assert read_session_reaper_row(direct_job)["codex_thread_id"] == direct_thread
    exception_job, _ = make_session_reaper_job(state="running", thread=False)
    with patch.object(delegate_router, "_route_agentic", return_value={"thread": direct_thread}), \
            patch.object(delegate_router, "_assemble_delegate_execution_result", side_effect=RuntimeError("fixture assembly failure")):
        delegate_router._run_delegate_job_to_completion(
            exception_job, "codex", "fixture task", None, None, REAPER_TEST_HOME, set(),
            result={}, classification={}, estimate={}, intent="session-retention-test")
    assert read_session_reaper_row(exception_job)["codex_thread_id"] == direct_thread
    assert read_session_reaper_row(exception_job)["state"] == "failed"
    early_job, _ = make_session_reaper_job(state="running", thread=False)
    early_execution = read_session_reaper_row(early_job)["execution_id"]
    delegate_jobs.record_delegate_codex_thread(direct_thread, early_execution)
    delegate_jobs.finish_delegate_job(early_job, state="failed", result_payload={"error": "transport died"})
    assert read_session_reaper_row(early_job)["codex_thread_id"] == direct_thread


def check_session_reaper_selection_and_retry():
    # Earlier capture fixtures must not enter this independent sweep.
    with delegate_jobs._open_delegate_jobs_connection() as connection:
        connection.execute("DELETE FROM delegate_jobs")
    due_done, done_thread = make_session_reaper_job(age=REAPER_TEST_MIDDLE_AGE_S)
    due_failed, failed_thread = make_session_reaper_job(state="failed")
    recent_failed, _ = make_session_reaper_job(state="failed", age=REAPER_TEST_MIDDLE_AGE_S)
    recent_done, _ = make_session_reaper_job(age=REAPER_TEST_NEW_AGE_S)
    running, _ = make_session_reaper_job(state="running")
    null_thread, _ = make_session_reaper_job(thread=False)
    with patch.object(config, "codex_session_reap_settings", return_value=REAPER_TEST_SETTINGS), \
            patch.object(codex_exec, "_bin", return_value=REAPER_TEST_BINARY), \
            patch.object(config, "lane_exec_env", return_value={}), \
            patch.object(delegate_jobs.subprocess, "run", return_value=SimpleNamespace(returncode=0, stdout="", stderr="")) as deletion:
        report = run_session_reaper_fixture()
        assert set(report["reaped"]) == {due_done, due_failed}
        assert {tuple(call.args[0]) for call in deletion.call_args_list} == {
            (REAPER_TEST_BINARY, "delete", done_thread, "--force"),
            (REAPER_TEST_BINARY, "delete", failed_thread, "--force")}
        for preserved in (recent_failed, recent_done, running, null_thread):
            assert read_session_reaper_row(preserved)["codex_session_reaped_at"] is None
        deletion.reset_mock()
        assert run_session_reaper_fixture()["reaped"] == []
        deletion.assert_not_called()
        retry_job, _ = make_session_reaper_job()
        deletion.return_value = SimpleNamespace(returncode=1, stdout="", stderr="permission denied")
        failed_report = run_session_reaper_fixture()
        assert failed_report["errors"] and read_session_reaper_row(retry_job)["codex_session_reaped_at"] is None
        deletion.return_value = SimpleNamespace(returncode=0, stdout="", stderr="")
        assert run_session_reaper_fixture()["reaped"] == [retry_job]
        for _ in range(REAPER_TEST_LIMIT + 1):
            make_session_reaper_job()
        with patch.object(config, "codex_session_reap_settings", return_value={**REAPER_TEST_SETTINGS, "max_per_pass": REAPER_TEST_LIMIT}):
            deletion.reset_mock()
            assert len(run_session_reaper_fixture()["reaped"]) == REAPER_TEST_LIMIT
            assert deletion.call_count == REAPER_TEST_LIMIT
        with patch.object(config, "codex_session_reap_settings", return_value={**REAPER_TEST_SETTINGS, "enabled": False}):
            deletion.reset_mock()
            assert run_session_reaper_fixture()["reaped"] == []
            deletion.assert_not_called()


def check_session_reaper_live_lease_safety():
    with delegate_jobs._open_delegate_jobs_connection() as connection:
        connection.execute("DELETE FROM delegate_jobs")
    held_job, held_thread = make_session_reaper_job()
    delegate_jobs.register_codex_session_lease(held_thread, os.getpid())
    with patch.object(config, "codex_session_reap_settings", return_value=REAPER_TEST_SETTINGS), \
            patch.object(codex_exec, "_bin", return_value=REAPER_TEST_BINARY), \
            patch.object(config, "lane_exec_env", return_value={}), \
            patch.object(delegate_jobs.subprocess, "run", return_value=SimpleNamespace(returncode=0, stdout="", stderr="")) as deletion:
        assert run_session_reaper_fixture()["reaped"] == []
        deletion.assert_not_called()
        assert read_session_reaper_row(held_job)["codex_session_reaped_at"] is None
        delegate_jobs.settle_codex_session_lease(held_thread, os.getpid())
        assert run_session_reaper_fixture()["reaped"] == []
        deletion.assert_not_called()
        with patch.object(warm_stdio_daemon, "_warm_owner_identity_state",
                          return_value=warm_stdio_daemon.WARM_OWNER_IDENTITY_DEAD):
            assert run_session_reaper_fixture()["reaped"] == [held_job]


def check_session_reaper_lease_transition():
    idle_thread, active_thread, next_thread = (str(uuid.uuid4()) for _ in range(3))
    delegate_jobs.register_codex_session_lease(idle_thread, os.getpid())
    delegate_jobs.settle_codex_session_lease(idle_thread, os.getpid())
    delegate_jobs.register_codex_session_lease(active_thread, os.getpid())
    delegate_jobs.register_codex_session_lease(next_thread, os.getpid())
    with delegate_jobs._open_delegate_jobs_connection() as connection:
        rows = connection.execute("SELECT thread_id FROM delegate_codex_session_leases WHERE worker_pid=?",
                                  (os.getpid(),)).fetchall()
    retained = {row[0] for row in rows}
    assert idle_thread not in retained
    assert {active_thread, next_thread} <= retained
    for session_id in (active_thread, next_thread):
        delegate_jobs.settle_codex_session_lease(session_id, os.getpid(), release=True)


def check_session_reaper_stale_lease_cleanup():
    with delegate_jobs._open_delegate_jobs_connection() as connection:
        connection.execute("DELETE FROM delegate_jobs")
        connection.execute("DELETE FROM delegate_codex_session_leases")
    orphan_session = str(uuid.uuid4())
    delegate_jobs.register_codex_session_lease(orphan_session, os.getpid())
    with patch.object(config, "codex_session_reap_settings", return_value=REAPER_TEST_SETTINGS), \
            patch.object(codex_exec, "_bin", return_value=REAPER_TEST_BINARY), \
            patch.object(delegate_jobs.subprocess, "run") as deletion, \
            patch.object(warm_stdio_daemon, "_warm_owner_identity_state",
                         return_value=warm_stdio_daemon.WARM_OWNER_IDENTITY_DEAD):
        assert run_session_reaper_fixture()["reaped"] == []
        deletion.assert_not_called()
    with delegate_jobs._open_delegate_jobs_connection() as connection:
        assert connection.execute("SELECT COUNT(*) FROM delegate_codex_session_leases").fetchone()[0] == 0


def check_session_reaper_shared_resume_safety():
    with delegate_jobs._open_delegate_jobs_connection() as connection:
        connection.execute("DELETE FROM delegate_jobs")
    old_job, session_id = make_session_reaper_job()
    running_job, _ = make_session_reaper_job(state="running")
    with delegate_jobs._open_delegate_jobs_connection() as connection:
        connection.execute("UPDATE delegate_jobs SET codex_thread_id=? WHERE job_id=?", (session_id, running_job))
    with patch.object(config, "codex_session_reap_settings", return_value=REAPER_TEST_SETTINGS), \
            patch.object(codex_exec, "_bin", return_value=REAPER_TEST_BINARY), \
            patch.object(config, "lane_exec_env", return_value={}), \
            patch.object(delegate_jobs.subprocess, "run") as deletion:
        assert run_session_reaper_fixture()["reaped"] == []
        deletion.assert_not_called()
        assert read_session_reaper_row(old_job)["codex_session_reaped_at"] is None


def check_session_reaper_missing_protocol():
    with delegate_jobs._open_delegate_jobs_connection() as connection:
        connection.execute("DELETE FROM delegate_jobs")
    with patch.object(config, "codex_session_reap_settings", return_value=REAPER_TEST_SETTINGS), \
            patch.object(codex_exec, "_bin", return_value=REAPER_TEST_BINARY), \
            patch.object(config, "lane_exec_env", return_value={}), \
            patch.object(delegate_jobs.subprocess, "run") as deletion:
        for template in delegate_jobs.CODEX_DELETE_MISSING_SESSION_TEMPLATES:
            missing_job, missing_thread = make_session_reaper_job()
            deletion.return_value = SimpleNamespace(returncode=1, stdout="",
                                                   stderr="Error: " + template.format(thread_id=missing_thread))
            report = run_session_reaper_fixture()
            assert report["reaped"] == [missing_job] and not report["errors"]
        wrong_job, _ = make_session_reaper_job()
        deletion.return_value = SimpleNamespace(returncode=1, stdout="", stderr="Error: " +
            delegate_jobs.CODEX_DELETE_MISSING_SESSION_TEMPLATES[0].format(thread_id=str(uuid.uuid4())))
        assert run_session_reaper_fixture()["errors"]
        assert read_session_reaper_row(wrong_job)["codex_session_reaped_at"] is None


def check_session_reaper_transport_capture():
    session_id = str(uuid.uuid4())
    events = "\n".join((json.dumps({"type": "thread.started", "thread_id": session_id}),
                         json.dumps({"type": "turn.failed", "error": {"message": "fixture failure"}})))
    assert codex_exec._codex_thread_from_events(events) == session_id
    assert codex_exec._codex_thread_from_events("") is None
    with patch.object(codex_exec, "_bin", return_value=REAPER_TEST_BINARY), \
            patch.object(config, "lane_exec_env", return_value={}), \
            patch.object(codex_exec, "_run_cold_codex_with_reaper",
                         return_value=SimpleNamespace(returncode=1, stdout=events, stderr="fixture failure")):
        result = codex_exec._run_agentic_codex_exec("fixture task", cwd=REAPER_TEST_HOME)
    assert result["thread"] == session_id and result["error"]
    timeout_error = codex_exec.subprocess.TimeoutExpired("mock-codex", REAPER_TEST_SETTINGS["delete_timeout_s"], output=events)
    with patch.object(codex_exec, "_bin", return_value=REAPER_TEST_BINARY), \
            patch.object(config, "lane_exec_env", return_value={}), \
            patch.object(codex_exec, "_run_cold_codex_with_reaper", side_effect=timeout_error):
        result = codex_exec._run_agentic_codex_exec("fixture task", cwd=REAPER_TEST_HOME)
    assert result["thread"] == session_id and result["error"]
    with patch.object(codex_exec, "_daemon_enabled", return_value=True), \
            patch.object(codex_daemon, "run_warm", return_value={"thread": session_id, "error": "fixture failure"}):
        result = codex_exec._run_prompt_in_cwd("fixture task", cwd=REAPER_TEST_HOME)
    assert result["thread"] == session_id and result["error"]

    def complete_reaper_cold_fixture(command, *args, **kwargs):
        output_index = command.index("--output-last-message") + 1
        Path(command[output_index]).write_text("complete fixture task", encoding="utf-8")
        return SimpleNamespace(returncode=0, stdout=events, stderr="")

    with patch.object(codex_exec, "_bin", return_value=REAPER_TEST_BINARY), \
            patch.object(config, "lane_exec_env", return_value={}), \
            patch.object(codex_exec, "_run_cold_codex_with_reaper", side_effect=complete_reaper_cold_fixture):
        result = codex_exec._run_agentic_codex_exec("fixture task", cwd=REAPER_TEST_HOME)
    assert result["thread"] == session_id and result["text"] == "complete fixture task"


check_session_reaper_migration()
check_session_reaper_terminal_capture()
check_session_reaper_selection_and_retry()
check_session_reaper_live_lease_safety()
check_session_reaper_lease_transition()
check_session_reaper_stale_lease_cleanup()
check_session_reaper_shared_resume_safety()
check_session_reaper_missing_protocol()
check_session_reaper_transport_capture()
print("codex session reaper contracts: all checks passed")
