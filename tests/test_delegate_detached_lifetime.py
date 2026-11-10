"""Exercise the real detached launcher boundary with an offline executor."""
import datetime
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
from unittest.mock import Mock, patch

import psutil

DETACHED_TEST_RECLAIM_RETENTION_S = 0
DETACHED_TEST_ZERO_WAIT_S = 0
DETACHED_TEST_COMPLETION_DELAY_S = 0.5
DETACHED_TEST_COMPLETION_BOUND_S = 15
DETACHED_TEST_POLL_S = 0.05
DETACHED_TEST_SOURCE_ROOT = Path(__file__).resolve().parents[1] / "src"
DETACHED_TEST_TESTS_ROOT = Path(__file__).resolve().parent
DETACHED_TEST_EDIT_FILENAME = "detached-result.txt"
DETACHED_TEST_EDIT_CONTENT = "isolated detached edit\n"
DETACHED_TEST_DELAY_ENV = "SPENDGUARD_DETACHED_TEST_DELAY_S"
DETACHED_TEST_GUARD_PREFIX = "sg-detached-join-guard-"
DETACHED_TEST_GUARD_EXECUTION_ID = "offline-guard-execution"


def remove_detached_test_worktree(repository, worktree, directory):
    """Drain writers before synchronously running the real scheduled reclaim."""
    from spendguard import config
    from spendguard.manage.token_operations import delegate_worktrees
    wait_for_detached_test_state_processes(directory)
    sidecars = list(directory.glob("delegate-executions/*/worktree.json"))
    assert sidecars, "missing retained workspace metadata"
    metadata = [json.loads(sidecar.read_text()) for sidecar in sidecars]
    # Cancellation may precede the terminal envelope's scheduling step. Complete
    # that same step after all writers exit, using the persisted workspace data.
    for sidecar, retained in zip(sidecars, metadata):
        if retained.get("reclaim_not_before"):
            continue
        delegate_worktrees.schedule_delegate_worktree_reclaim(
            {"worktree": retained}, metadata_writer=lambda updated, path=sidecar: path.write_text(json.dumps(updated)),
            retention_s=DETACHED_TEST_RECLAIM_RETENTION_S)
    # Advance the reaper's injected clock to the actual retention floor. No wall
    # clock delay or directory deletion can substitute for successful reclamation.
    reclaim_at = max(datetime.datetime.fromisoformat(retained["reclaim_not_before"]) for retained in metadata)
    report = delegate_worktrees.reclaim_expired_delegate_worktrees(
        now=reclaim_at, journal_root_resolver=lambda: directory / "delegate-executions",
        metadata_updater=config.update_json, worktree_root=Path(worktree["path"]).parent)
    assert not report["gaps"], report
    assert report["reclaimed"] == len(sidecars), report
    assert not Path(worktree["path"]).exists(), worktree
    assert all(json.loads(sidecar.read_text())["retained"] is False for sidecar in sidecars)
    registered = subprocess.run(["git", "-C", str(repository), "worktree", "list", "--porcelain"],
                                capture_output=True, text=True, check=True)
    assert worktree["path"] not in registered.stdout, registered.stdout


def wait_for_detached_test_state_processes(directory):
    """Join every recorded writer, including the detached journal observer."""
    state_paths = list(directory.glob("delegate-executions/*/state.json"))
    identities = set()
    for state_path in state_paths:
        state = json.loads(state_path.read_text())
        assert state.get("observer_pid") and state.get("observer_birth"), state
        identities.update((state.get(f"{role}_pid"), state.get(f"{role}_birth"))
                          for role in ("worker", "child", "observer"))
    deadline = time.monotonic() + DETACHED_TEST_COMPLETION_BOUND_S
    for process_id, birth in identities:
        if not process_id:
            continue
        assert birth is not None, f"missing birth identity for detached process {process_id}"
        try:
            process = psutil.Process(process_id)
            if process.create_time() != birth:
                continue  # The original writer exited and its PID was reused.
            process.wait(timeout=max(DETACHED_TEST_ZERO_WAIT_S, deadline - time.monotonic()))
        except psutil.NoSuchProcess:
            pass
        except psutil.TimeoutExpired as error:
            raise AssertionError(f"detached process {process_id} did not exit before cleanup") from error


def check_detached_process_join_guard():
    """Prove every writer is joined and a live writer times out loudly."""
    with tempfile.TemporaryDirectory(prefix=DETACHED_TEST_GUARD_PREFIX) as temporary:
        state_directory = Path(temporary)
        execution_directory = state_directory / "delegate-executions" / DETACHED_TEST_GUARD_EXECUTION_ID
        execution_directory.mkdir(parents=True)
        identity = psutil.Process()
        (execution_directory / "state.json").write_text(json.dumps({
            "observer_pid": identity.pid, "observer_birth": identity.create_time()}))
        joined = Mock()
        joined.create_time.return_value = identity.create_time()
        with patch.object(psutil, "Process", return_value=joined):
            wait_for_detached_test_state_processes(state_directory)
        joined.wait.assert_called_once()
        joined.wait.side_effect = psutil.TimeoutExpired(DETACHED_TEST_COMPLETION_BOUND_S)
        with patch.object(psutil, "Process", return_value=joined):
            try:
                wait_for_detached_test_state_processes(state_directory)
            except AssertionError:
                pass
            else:
                raise AssertionError("detached process join did not fail at its completion bound")
    print("2 passed: detached writer join waits for exit and times out loudly")


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

    def execute_detached_lifetime_fixture(workspace):
        execution_id = execution_journal.current_durable_execution_id()
        process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(float(__import__('sys').argv[1]))",
                                    os.environ.get(DETACHED_TEST_DELAY_ENV, str(DETACHED_TEST_COMPLETION_DELAY_S))])
        execution_journal.bind_durable_execution_child(execution_id, process.pid)
        process.wait()
        Path(workspace, DETACHED_TEST_EDIT_FILENAME).write_text(DETACHED_TEST_EDIT_CONTENT)
        return {"text": "offline detached result", "execution_started": True}

    delegate_router._route_agentic = lambda plan, prompt, model, timeout, cwd, **kwargs: (
        delegate_router._invoke_recoverable_delegate(
            lambda: delegate_router._run_isolated_agentic_provider(execute_detached_lifetime_fixture, cwd),
            plan=plan, model=model, cwd=cwd))
    def assemble_detached_lifetime_result(execution, **kwargs):
        delegate_router._schedule_terminal_delegate_workspace_reclaim(execution)
        return {"status": "executed", "execution": execution}
    delegate_router._assemble_delegate_execution_result = assemble_detached_lifetime_result


def check_detached_lifetime_boundary(cancel_isolated_job=False):
    with tempfile.TemporaryDirectory(prefix="sg-detached-lifetime-") as temporary:
        directory = Path(temporary)
        repository = directory / "repository"
        repository.mkdir()
        subprocess.run(["git", "init", str(repository)], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(repository), "-c", "user.name=Detached fixture",
                        "-c", "user.email=detached@example.invalid", "commit", "--allow-empty",
                        "-m", "Detached lifecycle baseline"], capture_output=True, check=True)
        # Python's standard startup hook injects the identical offline executor into
        # both fresh interpreters. Production still executes its normal provider path.
        (directory / "sitecustomize.py").write_text(
            "from test_delegate_detached_lifetime import install_detached_lifetime_fixture\n"
            "install_detached_lifetime_fixture()\n", encoding="utf-8")
        environment = {**os.environ, "SPENDGUARD_HOME": temporary,
                       "SPENDGUARD_TEST_ISOLATED": "1", "SPENDGUARD_NO_AUTOINSTALL": "1",
                       "PYTHONPATH": os.pathsep.join(map(str, (directory, DETACHED_TEST_TESTS_ROOT,
                                                                DETACHED_TEST_SOURCE_ROOT)))}
        if cancel_isolated_job:
            environment[DETACHED_TEST_DELAY_ENV] = str(DETACHED_TEST_COMPLETION_BOUND_S)
        detached = os.environ.get("SPENDGUARD_TEST_MUTATE_DAEMON_THREAD") != "1"
        launch = subprocess.run([
            sys.executable, "-c",
            "import json,os; from spendguard import delegate_router; "
            "print(json.dumps(delegate_router.start_delegate_job('offline task', cwd=os.getcwd(), "
            f"detached={detached!r})))"], env=environment, capture_output=True, text=True,
            timeout=DETACHED_TEST_COMPLETION_BOUND_S, check=True, cwd=repository)
        job_id = json.loads(launch.stdout)["job_id"]
        if cancel_isolated_job:
            cancellation = subprocess.run([
                sys.executable, "-c",
                "import json,sys,time; from spendguard import delegate_jobs; "
                "deadline=time.monotonic()+float(sys.argv[2]); status={}; "
                "exec('while time.monotonic()<deadline:\\n status=delegate_jobs.read_delegate_job(sys.argv[1])"
                "\\n if status.get(\"worktree\"): break\\n time.sleep(float(sys.argv[3]))'); "
                "assert status.get('worktree'), status; "
                "print(json.dumps(delegate_jobs.cancel_delegate_job(sys.argv[1])))",
                job_id, str(DETACHED_TEST_COMPLETION_BOUND_S), str(DETACHED_TEST_POLL_S)],
                env=environment, capture_output=True, text=True, timeout=DETACHED_TEST_COMPLETION_BOUND_S * 2,
                check=True)
            assert json.loads(cancellation.stdout)["state"] == "killed", cancellation.stdout
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
        if cancel_isolated_job:
            assert result["state"] == "killed", result
            retained = result["worktree"]
            assert retained["retained"] is True and Path(retained["path"]).is_dir(), result
            assert not (repository / DETACHED_TEST_EDIT_FILENAME).exists()
            assert Path(retained["path"]) != repository
            remove_detached_test_worktree(repository, retained, directory)
            print("4 passed: detached isolated cancel, retained collect metadata, primary unchanged, separate workspace")
            return
        assert isinstance(result, dict) and result.get("execution", {}).get("text") == "offline detached result", result
        states = list(directory.glob("delegate-executions/*/state.json"))
        assert len(states) == 1, states
        state = json.loads(states[0].read_text())
        assert state["child_pid"] and state["child_birth"], state
        assert state["worker_pid"] and state["worker_birth"], state
        assert state["state"] == "success", state
        worktree = result["execution"]["worktree"]
        assert Path(worktree["path"]) != repository and Path(worktree["path"]).exists(), worktree
        assert worktree["retained"] is True and worktree["reclaim_not_before"], worktree
        assert DETACHED_TEST_EDIT_FILENAME in worktree["changed_files"], worktree
        assert DETACHED_TEST_EDIT_CONTENT.strip() in worktree["patch"], worktree
        assert not (repository / DETACHED_TEST_EDIT_FILENAME).exists()
        persisted = json.loads((states[0].parent / "worktree.json").read_text())
        assert persisted == worktree, persisted
        remove_detached_test_worktree(repository, worktree, directory)
        print("9 passed: detached completion/collection, PID identity, terminal signal, retained isolated patch")


def check_explicit_warm_delegate_transport():
    """Warm requests reach the existing broker; defaults retain agentic MCP transport."""
    sys.path.insert(0, str(DETACHED_TEST_SOURCE_ROOT))
    from spendguard import codex_exec, delegate_router
    with patch.object(delegate_router.lane_registry, "lane_spec", return_value={"exec": "codex_exec", "agentic": True}), \
            patch.object(delegate_router, "_invoke_recoverable_delegate", side_effect=lambda executor, **kwargs: executor()), \
            patch("spendguard.delegate_worktrees.run_isolated_delegate", side_effect=lambda executor, cwd, **kwargs: executor(cwd)), \
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
            adopted_worker = subprocess.Popen([
                sys.executable, "-c", "import time,sys; time.sleep(float(sys.argv[1]))",
                str(DETACHED_TEST_COMPLETION_DELAY_S),
            ], start_new_session=True)

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
            execution_journal.adopt_durable_execution_owner(execution_id, adopted_worker.pid)
            execution_journal.terminalize_durable_execution(execution_id, "killed")
            adopted_worker.wait(timeout=DETACHED_TEST_COMPLETION_BOUND_S)
            wait_for_detached_test_state_processes(Path(temporary))
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
    sys.path.insert(0, str(DETACHED_TEST_SOURCE_ROOT))
    check_detached_process_join_guard()
    check_detached_lifetime_boundary()
    check_detached_lifetime_boundary(cancel_isolated_job=True)
    check_detached_adoption_rejects_stale_loss()
    check_explicit_warm_delegate_transport()
    check_warm_cancellation_stops_retry()
