"""A watchdog terminal must remain authoritative in the delegate job projection."""
import os
import json
from pathlib import Path
import sys
from unittest.mock import patch

from _pytest_fixture_bridge import align_test_startup_home

TERMINAL_PROJECTION_HOME = align_test_startup_home("sg-terminal-projection-")
os.environ.setdefault("SPENDGUARD_TEST_ISOLATED", "1")
os.environ.setdefault("SPENDGUARD_NO_AUTOINSTALL", "1")
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from spendguard import delegate_jobs, execution_journal  # noqa: E402


def test_watchdog_winner_controls_job_projection():
    projection_job_id = delegate_jobs.create_delegate_job(
        kind="agentic", task_fingerprint=__name__, cwd=os.getcwd(), provider=None,
        intent=None, dirty_before=set())
    projection_execution_id = delegate_jobs._delegate_job_row(projection_job_id)["execution_id"]
    # Exact interleaving: watchdog persists its terminal, then normal completion
    # runs before the watchdog's recover_delegate_job_execution projection.
    execution_journal.terminalize_durable_execution(projection_execution_id, "lost")
    delegate_jobs.finish_delegate_job(projection_job_id, state="done", result_payload={"status": "executed"})
    delegate_jobs.recover_delegate_job_execution(projection_job_id)
    projection_record = execution_journal.recover_durable_execution(projection_execution_id)
    projection_row = delegate_jobs._delegate_job_row(projection_job_id)
    assert projection_record["state"] == "lost"
    assert projection_row["state"] == delegate_jobs.DELEGATE_JOB_LOST_STATE, dict(projection_row)


def test_captured_completion_supersedes_loss_without_terminal_flapping():
    for loss_first, captured_exit_code in ((True, 0), (False, 0), (True, None), (False, None)):
        authority_job_id = delegate_jobs.create_delegate_job(
            kind="agentic", task_fingerprint=__name__, cwd=os.getcwd(), provider=None,
            intent=None, dirty_before=set())
        authority_execution_id = delegate_jobs._delegate_job_row(authority_job_id)["execution_id"]
        if loss_first:
            execution_journal.terminalize_durable_execution(authority_execution_id, "lost")
            delegate_jobs.recover_delegate_job_execution(authority_job_id)
        execution_journal.persist_durable_execution_payload(
            authority_execution_id, result=__name__, exit_code=captured_exit_code)
        delegate_jobs.finish_delegate_job(
            authority_job_id, state="done", result_payload={"status": "executed", "text": __name__})
        execution_journal.terminalize_durable_execution(authority_execution_id, "lost")
        delegate_jobs.finish_delegate_job(authority_job_id, state="lost")
        authority_record = execution_journal.recover_durable_execution(authority_execution_id)
        assert authority_record["state"] == "success"
        assert authority_record["terminal_authority"] == execution_journal.EXECUTION_CAPTURED_AUTHORITY
        assert authority_record["result"] == __name__
        assert delegate_jobs._delegate_job_row(authority_job_id)["state"] == delegate_jobs.DELEGATE_JOB_DONE_STATE
        assert delegate_jobs.read_delegate_job_result(authority_job_id)["text"] == __name__
        authority_directory = execution_journal.durable_execution_payload_paths(authority_execution_id)["result"].parent
        for authority_events_path in (authority_directory / execution_journal.EXECUTION_EVENTS_FILENAME,
                                      Path(authority_record["metadata"]["ledger_path"])):
            authority_ends = [json.loads(line) for line in authority_events_path.read_text().splitlines()
                              if json.loads(line).get("execution_id") == authority_execution_id
                              and json.loads(line).get("phase") == "end"]
            assert len(authority_ends) == 1
            assert authority_ends[0]["state"] == "success"


def test_authoritative_end_repairs_interrupted_state_projection():
    interrupted_execution_id = execution_journal.begin_durable_execution()
    execution_journal.terminalize_durable_execution(interrupted_execution_id, "lost")
    with patch.object(execution_journal, "_write_durable_execution_state", side_effect=OSError(__name__)):
        try:
            execution_journal.terminalize_durable_execution(
                interrupted_execution_id, "success", result=__name__, exit_code=0)
        except execution_journal.DurableExecutionPersistenceError:
            pass
        else:
            raise AssertionError("state persistence failure was hidden")
    interrupted_recovery = execution_journal.recover_durable_execution(interrupted_execution_id)
    assert interrupted_recovery["state"] == "success"
    assert interrupted_recovery["exit_code"] == 0
    assert interrupted_recovery["result"] == __name__


def test_failed_atomic_terminal_replacement_preserves_complete_ledger():
    replacement_execution_id = execution_journal.begin_durable_execution()
    execution_journal.terminalize_durable_execution(replacement_execution_id, "lost")
    replacement_directory = execution_journal.durable_execution_payload_paths(replacement_execution_id)["result"].parent
    replacement_events_path = replacement_directory / execution_journal.EXECUTION_EVENTS_FILENAME
    original_replacement_events = replacement_events_path.read_bytes()
    with patch.object(execution_journal.os, "replace", side_effect=OSError(__name__)):
        try:
            execution_journal.terminalize_durable_execution(
                replacement_execution_id, "success", result=__name__, exit_code=0)
        except execution_journal.DurableExecutionPersistenceError:
            pass
        else:
            raise AssertionError("terminal replacement failure was hidden")
    assert replacement_events_path.read_bytes() == original_replacement_events
    assert execution_journal.recover_durable_execution(replacement_execution_id)["result"] == __name__


if __name__ == "__main__":
    from _pytest_fixture_bridge import run_pytest_style
    sys.exit(run_pytest_style(globals()))
