"""Exercise the real detached launcher boundary with an offline executor."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
from unittest.mock import patch

DETACHED_TEST_COMPLETION_DELAY_S = 0.5
DETACHED_TEST_COMPLETION_BOUND_S = 15
DETACHED_TEST_POLL_S = 0.05
DETACHED_TEST_SOURCE_ROOT = Path(__file__).resolve().parents[1] / "src"
DETACHED_TEST_TESTS_ROOT = Path(__file__).resolve().parent


def install_detached_lifetime_fixture():
    """Replace provider work, leaving process, journal, and terminal storage real."""
    import spendguard
    from spendguard import delegate_router, execution_journal
    spendguard.require = lambda: None  # Provider is replaced wholesale below; no model call can run.
    delegate_router.delegate_task = lambda *args, **kwargs: {
        "status": "estimate", "classification": {"kind": "agentic"},
        "estimate": {"plan": "offline-lifetime-provider", "model": "offline-lifetime-model"}}
    delegate_router.delegate_route_fingerprint = lambda *args, **kwargs: "offline-lifetime-fingerprint"
    delegate_router._worktree_state = lambda cwd: (None, set())
    delegate_router._worktree_changed_files = lambda cwd, before: []

    def execute_detached_lifetime_fixture(*args, **kwargs):
        execution_id = execution_journal.current_durable_execution_id()
        process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(float(__import__('sys').argv[1]))",
                                    str(DETACHED_TEST_COMPLETION_DELAY_S)])
        execution_journal.bind_durable_execution_child(execution_id, process.pid)
        process.wait()
        return {"text": "offline detached result", "execution_started": True}

    delegate_router._route_agentic = execute_detached_lifetime_fixture
    delegate_router._assemble_delegate_execution_result = lambda execution, **kwargs: {
        "status": "executed", "execution": execution}


def check_detached_lifetime_boundary():
    with tempfile.TemporaryDirectory(prefix="sg-detached-lifetime-") as temporary:
        directory = Path(temporary)
        # Python's standard startup hook injects the identical offline executor into
        # both fresh interpreters. Production still executes its normal provider path.
        (directory / "sitecustomize.py").write_text(
            "from test_delegate_detached_lifetime import install_detached_lifetime_fixture\n"
            "install_detached_lifetime_fixture()\n", encoding="utf-8")
        environment = {**os.environ, "SPENDGUARD_HOME": temporary,
                       "SPENDGUARD_TEST_ISOLATED": "1", "SPENDGUARD_NO_AUTOINSTALL": "1",
                       "PYTHONPATH": os.pathsep.join(map(str, (directory, DETACHED_TEST_TESTS_ROOT,
                                                                DETACHED_TEST_SOURCE_ROOT)))}
        detached = os.environ.get("SPENDGUARD_TEST_MUTATE_DAEMON_THREAD") != "1"
        launch = subprocess.run([
            sys.executable, "-c",
            "import json,os; from spendguard import delegate_router; "
            "print(json.dumps(delegate_router.start_delegate_job('offline task', cwd=os.getcwd(), "
            f"detached={detached!r})))"], env=environment, capture_output=True, text=True,
            timeout=DETACHED_TEST_COMPLETION_BOUND_S, check=True)
        job_id = json.loads(launch.stdout)["job_id"]
        # This interpreter is deliberately launched only AFTER the launcher exited.
        collector = subprocess.run([
            sys.executable, "-c",
            "import json,sys,time; from spendguard import delegate_jobs; "
            "deadline=time.monotonic()+float(sys.argv[2]); result={}; "
            "exec('while time.monotonic()<deadline:\\n result=delegate_jobs.read_delegate_job_result(sys.argv[1])"
            "\\n if not isinstance(result,dict) or result.get(\"status\") != \"running\": break\\n time.sleep(float(sys.argv[3]))'); "
            "print(json.dumps(result))", job_id, str(DETACHED_TEST_COMPLETION_BOUND_S),
            str(DETACHED_TEST_POLL_S)], env=environment, capture_output=True, text=True,
            timeout=DETACHED_TEST_COMPLETION_BOUND_S * 2, check=True)
        result = json.loads(collector.stdout)
        assert isinstance(result, dict) and result.get("execution", {}).get("text") == "offline detached result", result
        states = list(directory.glob("delegate-executions/*/state.json"))
        assert len(states) == 1, states
        state = json.loads(states[0].read_text())
        assert state["child_pid"] and state["child_birth"], state
        assert state["worker_pid"] and state["worker_birth"], state
        assert state["state"] == "success", state
        print("4 passed: detached completion, collect after exit, child identity, terminal signal")


def check_explicit_warm_delegate_transport():
    """Warm requests reach the existing broker; defaults retain agentic MCP transport."""
    sys.path.insert(0, str(DETACHED_TEST_SOURCE_ROOT))
    from spendguard import codex_exec, delegate_router
    with patch.object(delegate_router.lane_registry, "lane_spec", return_value={"exec": "codex_exec"}), \
            patch.object(delegate_router, "_invoke_recoverable_delegate", side_effect=lambda executor, **kwargs: executor()), \
            patch.object(codex_exec, "run_prompt", side_effect=lambda prompt, **kwargs: kwargs):
        default_transport = delegate_router._route_agentic("offline-plan", "offline prompt", None, None,
                                                         str(DETACHED_TEST_SOURCE_ROOT))
        warm_transport = delegate_router._route_agentic("offline-plan", "offline prompt", None, None,
                                                      str(DETACHED_TEST_SOURCE_ROOT), warm=True)
    assert default_transport["delegate_context"] is True, default_transport
    assert warm_transport["delegate_context"] is False, warm_transport
    print("2 passed: default transport preserved, explicit warm reaches broker")


def check_detached_adoption_rejects_stale_loss():
    """A stale launcher-death observation cannot terminalize its adopted worker."""
    from spendguard import execution_journal
    with tempfile.TemporaryDirectory(prefix="sg-detached-adoption-") as temporary:
        with patch.object(execution_journal, "durable_execution_store_root", return_value=Path(temporary)):
            execution_id = execution_journal.begin_durable_execution()
            original_terminalize = execution_journal.terminalize_durable_execution

            def transfer_before_terminalize(execution_id, state, **kwargs):
                with execution_journal._locked_durable_execution(execution_id) as directory:
                    record = json.loads((directory / execution_journal.EXECUTION_STATE_FILENAME).read_text())
                    record["owner_birth"] = None
                    execution_journal._write_durable_execution_state(directory, record)
                return original_terminalize(execution_id, state, **kwargs)

            with patch.object(execution_journal, "_durable_execution_has_live_process", return_value=False), \
                    patch.object(execution_journal, "terminalize_durable_execution",
                                 side_effect=transfer_before_terminalize):
                recovered = execution_journal.recover_durable_execution(execution_id)
            assert recovered["state"] == "running", recovered
            execution_journal.adopt_durable_execution_owner(execution_id)
            execution_journal.terminalize_durable_execution(execution_id, "killed")
    print("1 passed: stale launcher observation cannot terminalize adopted worker")


def check_warm_cancellation_stops_retry():
    """A killed leased app-server must not restart the cancelled delegate."""
    from spendguard import codex_daemon, execution_journal
    daemon = codex_daemon._CodexDaemon()
    daemon._cancel_idle_shutdown = lambda: None
    daemon._schedule_idle_shutdown = lambda: None
    daemon._enter_warm_evidence_scope = lambda: "offline-warm-execution"
    daemon._leave_warm_evidence_scope = lambda execution_id: None
    daemon._reference_warm_stderr_archive = lambda *args: None
    with patch.object(daemon, "ensure_running", return_value=SimpleNamespace(pid=os.getpid())) as ensure, \
            patch.object(daemon, "shutdown"), \
            patch.object(daemon, "_write_rpc", side_effect=BrokenPipeError), \
            patch.object(execution_journal, "recover_durable_execution",
                         side_effect=({"state": "running"}, {"state": "running"}, {"state": "killed"})), \
            patch.object(execution_journal, "bind_durable_execution_child") as bind:
        result = daemon.run_warm("offline warm task", sandbox="workspace-write")
    assert result.get("error") == "delegate execution cancelled", result
    assert ensure.call_count == 1, ensure.call_count
    bind.assert_called_once_with("offline-warm-execution", os.getpid())
    print("3 passed: warm child recorded, cancelled turn fails, no retry after kill")


if __name__ == "__main__":
    check_detached_lifetime_boundary()
    check_detached_adoption_rejects_stale_loss()
    check_explicit_warm_delegate_transport()
    check_warm_cancellation_stops_retry()
