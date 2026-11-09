"""Regression guards for terminal visibility after delegate artifacts are durable."""
import contextlib
import json
import os
from pathlib import Path
import sys
from unittest import mock

from _pytest_fixture_bridge import align_test_startup_home

DELEGATE_ARTIFACT_ORDER_ROOT = Path(align_test_startup_home("sg-delegate-artifact-order-"))
os.environ.setdefault("SPENDGUARD_TEST_ISOLATED", "1")
os.environ.setdefault("SPENDGUARD_NO_AUTOINSTALL", "1")
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from spendguard.manage.token_operations import delegate_jobs, execution_journal  # noqa: E402


def create_delegate_artifact_order_job(task_fingerprint):
    return delegate_jobs.create_delegate_job(
        kind="agentic", task_fingerprint=task_fingerprint, cwd=os.getcwd(), provider=None,
        intent=None, dirty_before=set())


def assert_terminal_journal_recovers_complete_artifacts_after_job_commit_failure():
    job_id = create_delegate_artifact_order_job("terminal-artifact-recovery")
    completed_payload = {
        "status": "executed",
        "execution": {"text": "complete work report", "worktree": {"patch": "complete patch"}},
        "changed_files": ["complete-artifact.txt"],
    }
    original_connection_factory = delegate_jobs._open_delegate_jobs_connection

    @contextlib.contextmanager
    def fail_terminal_job_commit():
        raise OSError("injected terminal job commit failure")
        yield

    with mock.patch.object(delegate_jobs, "_open_delegate_jobs_connection", fail_terminal_job_commit):
        try:
            delegate_jobs.finish_delegate_job(job_id, state="done", result_payload=completed_payload)
        except OSError as error:
            assert str(error) == "injected terminal job commit failure"
        else:
            raise AssertionError("injected terminal job commit failure was not reported")

    assert delegate_jobs._delegate_job_row(job_id)["state"] == delegate_jobs.DELEGATE_JOB_RUNNING_STATE
    with mock.patch.object(delegate_jobs, "_open_delegate_jobs_connection", original_connection_factory):
        row = delegate_jobs._delegate_job_row(job_id)
        recovered = execution_journal.recover_durable_execution(row["execution_id"])
    assert recovered["state"] == "success"
    assert json.loads(recovered["result"]) == completed_payload


def assert_journal_persistence_failure_does_not_publish_terminal_job_state():
    job_id = create_delegate_artifact_order_job("terminal-persistence-failure")
    completed_payload = {"status": "executed", "execution": {"text": "complete work report"}}
    with mock.patch.object(
            execution_journal, "terminalize_durable_execution",
            side_effect=execution_journal.DurableExecutionPersistenceError(
                "injected lifecycle persistence failure")):
        try:
            delegate_jobs.finish_delegate_job(job_id, state="done", result_payload=completed_payload)
        except execution_journal.DurableExecutionPersistenceError as error:
            assert str(error) == "injected lifecycle persistence failure"
        else:
            raise AssertionError("injected lifecycle persistence failure was not reported")
    assert delegate_jobs._delegate_job_row(job_id)["state"] == delegate_jobs.DELEGATE_JOB_RUNNING_STATE


if __name__ == "__main__":
    assert_terminal_journal_recovers_complete_artifacts_after_job_commit_failure()
    assert_journal_persistence_failure_does_not_publish_terminal_job_state()
    print("delegate terminal artifact ordering: all checks passed")
