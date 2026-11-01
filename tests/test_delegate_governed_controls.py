"""Target the governed lifecycle using real local workers; no provider calls."""
import contextlib
import errno
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from unittest import mock

from _pytest_fixture_bridge import align_test_startup_home

CONTROLS_TEST_HOME = Path(align_test_startup_home("sg-governed-controls-"))
os.environ.setdefault("SPENDGUARD_TEST_ISOLATED", "1")
os.environ.setdefault("SPENDGUARD_NO_AUTOINSTALL", "1")
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from spendguard import delegate_jobs, delegate_router, execution_journal, process_group_reaper  # noqa: E402
from test_delegate_cancel_process_group import (  # noqa: E402
    PROCESS_GROUP_TEST_EPERM_SKIP_REASON,
    host_permits_foreign_session_killpg,
)

CONTROLS_PROCESS_BOUND_S = 5
CONTROLS_FINISH_DELAY_S = 0.15
CONTROLS_WAIT_TIMEOUT_S = 0.02
CONTROLS_IDLE_PROCESS_CODE = "import sys; print('ready', flush=True); sys.stdin.read()"
CONTROLS_MUTATION_ENV = "SPENDGUARD_CONTROLS_MUTATION"
CONTROLS_CANCEL_SKIP_ORPHAN_GROUP_MUTATION = "cancel-skip-orphan-group"
CONTROLS_ORPHAN_REPOSITORY_DIRECTORY_NAME = "repository"
CONTROLS_ORPHAN_TEMPORARY_DIRECTORY_PREFIX = "sg-governed-orphan-group-"
CONTROLS_ORPHAN_WRITER_FILENAME_PREFIX = "write-"
CONTROLS_ORPHAN_WRITER_POLL_S = 0.01
CONTROLS_ORPHAN_WORKER_CODE = """
import pathlib,signal,subprocess,sys
repository=pathlib.Path(sys.argv[1])
writer_code=sys.argv[2]
child=subprocess.Popen([sys.executable, '-c', writer_code, str(repository), sys.argv[3], sys.argv[4]])
print(child.pid, flush=True)
sys.stdin.readline()
"""
CONTROLS_ORPHAN_WRITER_CODE = """
import pathlib,signal,sys,time
repository=pathlib.Path(sys.argv[1])
signal.signal(signal.SIGTERM, signal.SIG_IGN)
counter=0
while True:
    (repository / (sys.argv[2] + str(counter))).write_text(str(counter))
    counter += 1
    time.sleep(float(sys.argv[3]))
"""


def create_controls_fixture_job():
    return delegate_jobs.create_delegate_job(
        kind="agentic", task_fingerprint=__name__, cwd=os.getcwd(),
        provider=None, intent=None, dirty_before=set())


def assert_controls_wait_and_finish_signal():
    waiting_job_id = create_controls_fixture_job()
    completion_payload = {"status": "executed", "execution": {"text": __name__}}
    terminal_written = threading.Event()

    def write_controls_delayed_terminal():
        time.sleep(CONTROLS_FINISH_DELAY_S)
        delegate_jobs.finish_delegate_job(waiting_job_id, state="done", result_payload=completion_payload)
        terminal_written.set()

    finisher = threading.Thread(target=write_controls_delayed_terminal)
    finisher.start()
    try:
        timeout_result = delegate_jobs.wait_for_delegate_job(waiting_job_id, timeout=CONTROLS_WAIT_TIMEOUT_S)
        assert timeout_result["state"] == "running" and timeout_result["wait_timed_out"]
        collected = delegate_jobs.wait_for_delegate_job(waiting_job_id, timeout=CONTROLS_PROCESS_BOUND_S)
        assert terminal_written.wait(CONTROLS_PROCESS_BOUND_S)
        assert collected == {**completion_payload, "state": "done"}, "wait returned before completion"
        # Inspect the actual persisted signals, without using recover/status to repair them.
        row = delegate_jobs._delegate_job_row(waiting_job_id)
        assert row["state"] == "done" and json.loads(row["result_payload"]) == completion_payload
        directory = execution_journal.durable_execution_payload_paths(row["execution_id"])["result"].parent
        state = json.loads((directory / execution_journal.EXECUTION_STATE_FILENAME).read_text())
        assert state["state"] == "success", "finish signal was not written promptly"
    finally:
        finisher.join(CONTROLS_PROCESS_BOUND_S)


def assert_controls_cancel_real_worker(force):
    with subprocess.Popen([sys.executable, "-c", CONTROLS_IDLE_PROCESS_CODE],
                          stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
                          start_new_session=True) as worker:
        try:
            assert worker.stdout.readline().strip() == "ready"
            cancelled_job_id = create_controls_fixture_job()
            execution_id = delegate_jobs._delegate_job_row(cancelled_job_id)["execution_id"]
            execution_journal.adopt_durable_execution_owner(execution_id, worker.pid)
            cancelled = delegate_jobs.cancel_delegate_job(cancelled_job_id, force=force)
            assert cancelled["state"] == "killed", cancelled
            assert worker.poll() is not None, "governed cancel left the real worker alive"
            assert delegate_jobs._delegate_job_row(cancelled_job_id)["state"] == "killed"
            assert delegate_router.wait_for_delegate_job_handoff(cancelled_job_id)["state"] == "killed"
            # Late completion must lose to cancellation, and repeat cancellation is idempotent.
            delegate_jobs.finish_delegate_job(cancelled_job_id, state="done", result_payload={"text": "late"})
            assert delegate_jobs.cancel_delegate_job(cancelled_job_id)["state"] == "killed"
        finally:
            if worker.poll() is None:
                worker.kill()
            worker.wait(timeout=CONTROLS_PROCESS_BOUND_S)


def assert_controls_cancel_reaps_orphaned_worker_group():
    temporary = Path(tempfile.mkdtemp(prefix=CONTROLS_ORPHAN_TEMPORARY_DIRECTORY_PREFIX))
    repository = temporary / CONTROLS_ORPHAN_REPOSITORY_DIRECTORY_NAME
    repository.mkdir()
    worker = subprocess.Popen(
        [sys.executable, "-c", CONTROLS_ORPHAN_WORKER_CODE, str(repository),
         CONTROLS_ORPHAN_WRITER_CODE, CONTROLS_ORPHAN_WRITER_FILENAME_PREFIX,
         str(CONTROLS_ORPHAN_WRITER_POLL_S)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, start_new_session=True)
    descendant_pid = int(worker.stdout.readline())
    try:
        orphaned_job_id = create_controls_fixture_job()
        execution_id = delegate_jobs._delegate_job_row(orphaned_job_id)["execution_id"]
        execution_journal.adopt_durable_execution_owner(execution_id, worker.pid)
        worker.stdin.close()
        worker.wait(timeout=CONTROLS_PROCESS_BOUND_S)
        assert os.getpgid(descendant_pid) == worker.pid
        os.killpg(worker.pid, process_group_reaper.PROCESS_GROUP_LIVENESS_PROBE_SIGNAL)
        mutation = os.environ.get(CONTROLS_MUTATION_ENV)
        mutation_context = (mock.patch.object(delegate_jobs, "reap_process_group_bounded", return_value=True)
                            if mutation == CONTROLS_CANCEL_SKIP_ORPHAN_GROUP_MUTATION
                            else contextlib.nullcontext())
        with mutation_context:
            cancelled = delegate_jobs.cancel_delegate_job(orphaned_job_id)
        assert isinstance(cancelled, dict) and cancelled["state"] == "killed", cancelled
        try:
            os.killpg(worker.pid, process_group_reaper.PROCESS_GROUP_LIVENESS_PROBE_SIGNAL)
        except ProcessLookupError as error:
            assert error.errno == errno.ESRCH
        else:
            raise AssertionError("cancel returned while an orphaned process-group descendant survived")
        shutil.rmtree(repository)
        assert not repository.exists()
    finally:
        try:
            os.killpg(worker.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        try:
            os.kill(descendant_pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        worker.wait(timeout=CONTROLS_PROCESS_BOUND_S)
        shutil.rmtree(temporary, ignore_errors=True)


def assert_controls_pid_reuse_protected():
    with subprocess.Popen([sys.executable, "-c", CONTROLS_IDLE_PROCESS_CODE],
                          stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True) as unrelated:
        try:
            assert unrelated.stdout.readline().strip() == "ready"
            protected_job_id = create_controls_fixture_job()
            execution_id = delegate_jobs._delegate_job_row(protected_job_id)["execution_id"]
            execution_journal.bind_durable_execution_child(execution_id, unrelated.pid)
            with execution_journal._locked_durable_execution(execution_id) as directory:
                record = json.loads((directory / execution_journal.EXECUTION_STATE_FILENAME).read_text())
                record["child_birth"] = None
                execution_journal._write_durable_execution_state(directory, record)
            protected = delegate_jobs.cancel_delegate_job(protected_job_id)
            assert protected.get("error") and unrelated.poll() is None
            delegate_jobs.finish_delegate_job(protected_job_id, state="failed")
        finally:
            unrelated.kill()
            unrelated.wait(timeout=CONTROLS_PROCESS_BOUND_S)


def assert_controls_canonical_terminal_key():
    key_job_id = create_controls_fixture_job()
    delegate_jobs.finish_delegate_job(key_job_id, state="done", result_payload={"status": "executed"})
    assert delegate_jobs.read_delegate_job(key_job_id)["state"] == delegate_jobs.read_delegate_job_result(key_job_id)["state"]
    assert delegate_jobs.read_delegate_job(key_job_id)["status"] == "done"
    assert delegate_jobs.read_delegate_job_result(key_job_id)["status"] == "executed"


with contextlib.ExitStack() as mutations:
    mutation = os.environ.get(CONTROLS_MUTATION_ENV)
    if mutation == "cancel-no-signal":
        import psutil
        mutations.enter_context(mock.patch.object(psutil.Process, "terminate"))
        mutations.enter_context(mock.patch.object(psutil.Process, "kill"))
        mutations.enter_context(mock.patch.object(psutil, "wait_procs", return_value=([], [])))
    elif mutation == "wait-no-block":
        mutations.enter_context(mock.patch.object(delegate_jobs, "wait_for_delegate_job",
                                                  delegate_jobs.read_delegate_job_result))
    elif mutation == "finish-no-terminal":
        mutations.enter_context(mock.patch.object(delegate_jobs, "finish_delegate_job"))
    elif mutation == "collect-without-state":
        original_controls_collect = delegate_jobs.read_delegate_job_result

        def mutated_controls_collect(job_id):
            payload = original_controls_collect(job_id)
            if isinstance(payload, dict):
                payload.pop("state", None)
            return payload

        mutations.enter_context(mock.patch.object(delegate_jobs, "read_delegate_job_result", mutated_controls_collect))
    if mutation == "wait-no-block":
        # Preserve deadline metadata while restoring the pre-fix immediate collect behavior.
        mutations.enter_context(mock.patch.object(
            delegate_jobs, "wait_for_delegate_job",
            lambda job_id, timeout=None: {**delegate_jobs.read_delegate_job_result(job_id), "wait_timed_out": True}))
    if mutation in (None, "wait-no-block", "finish-no-terminal"):
        assert_controls_wait_and_finish_signal()
    if mutation in (None, "cancel-no-signal", CONTROLS_CANCEL_SKIP_ORPHAN_GROUP_MUTATION):
        if host_permits_foreign_session_killpg():
            if mutation in (None, "cancel-no-signal"):
                assert_controls_cancel_real_worker(force=False)
                assert_controls_cancel_real_worker(force=True)
            if mutation in (None, CONTROLS_CANCEL_SKIP_ORPHAN_GROUP_MUTATION):
                assert_controls_cancel_reaps_orphaned_worker_group()
        else:
            print(PROCESS_GROUP_TEST_EPERM_SKIP_REASON)
        assert_controls_pid_reuse_protected()
    if mutation in (None, "collect-without-state"):
        assert_controls_canonical_terminal_key()

print("governed delegate controls: 8 targeting contracts passed")
