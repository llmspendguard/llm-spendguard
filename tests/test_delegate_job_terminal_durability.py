"""Offline reproduction of immutable terminals and recovery after an owner dies."""
import os
from pathlib import Path
import subprocess
import sys
import time
from _pytest_fixture_bridge import align_test_startup_home

JOB_DURABILITY_ROOT = Path(align_test_startup_home("sg-job-terminal-"))
os.environ.setdefault("SPENDGUARD_TEST_ISOLATED", "1")
os.environ.setdefault("SPENDGUARD_NO_AUTOINSTALL", "1")
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from spendguard import delegate_jobs  # noqa: E402

JOB_DURABILITY_WAIT_S = 10
JOB_DURABILITY_POLL_S = 0.01
JOB_DURABILITY_CHILD_CODE = "import sys; sys.stdout.write('ready\\n'); sys.stdout.flush(); sys.stdin.read()"


def reproduce_delegate_job_terminal_immutable():
    terminal_job = delegate_jobs.create_delegate_job(
        kind="agentic", task_fingerprint="immutable", cwd=os.getcwd(), provider=None,
        intent=None, dirty_before=set())
    first_payload = {"status": "executed", "execution": {"text": "original complete output"}}
    delegate_jobs.finish_delegate_job(terminal_job, state="done", result_payload=first_payload)
    delegate_jobs.finish_delegate_job(terminal_job, state="failed", result_payload={"error": "late finalizer"})
    assert delegate_jobs.read_delegate_job_result(terminal_job) == {**first_payload, "state": "done"}


def reproduce_delegate_job_dead_owner_terminal(journal_completed=False):
    with subprocess.Popen([sys.executable, "-c", JOB_DURABILITY_CHILD_CODE],
                          stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True) as owner:
        try:
            killed_job = delegate_jobs.create_delegate_job(
                kind="agentic", task_fingerprint="dead-owner", cwd=os.getcwd(), provider=None,
                intent=None, dirty_before=set(), owner_pid=owner.pid)
            if journal_completed:
                from spendguard.execution_journal import terminalize_durable_execution
                terminalize_durable_execution(delegate_jobs._delegate_job_row(killed_job)["execution_id"],
                                             "success", result="completed before owner died", exit_code=0)
        finally:
            owner.kill()
            owner.wait(timeout=JOB_DURABILITY_WAIT_S)
    terminal_deadline = time.monotonic() + JOB_DURABILITY_WAIT_S
    while delegate_jobs._delegate_job_row(killed_job)["state"] == "running":
        assert time.monotonic() < terminal_deadline, "watchdog left an orphan running job"
        time.sleep(JOB_DURABILITY_POLL_S)
    recovered_job = delegate_jobs.recover_delegate_job_execution(killed_job)
    expected_state = "done" if journal_completed else "lost"
    assert recovered_job["state"] == expected_state
    assert delegate_jobs.read_delegate_job(killed_job)["state"] == expected_state
    assert delegate_jobs.find_inflight_delegate_job("dead-owner", os.getcwd()) is None


reproduce_delegate_job_terminal_immutable()
reproduce_delegate_job_dead_owner_terminal()
reproduce_delegate_job_dead_owner_terminal(journal_completed=True)
print("delegate job terminal durability: all checks passed")
