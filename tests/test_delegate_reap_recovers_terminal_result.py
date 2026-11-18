"""Pre-fix failure: reaping changed the job to reclaimed and discarded its terminal result."""
import datetime
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

DELEGATE_REAP_RECOVERY_HOME_PREFIX = "sg-delegate-reap-recovery-"
DELEGATE_REAP_RECOVERY_HOME = tempfile.mkdtemp(prefix=DELEGATE_REAP_RECOVERY_HOME_PREFIX)
os.environ.setdefault("SPENDGUARD_HOME", DELEGATE_REAP_RECOVERY_HOME)
os.environ.setdefault("SPENDGUARD_TEST_ISOLATED", "1")
os.environ.setdefault("SPENDGUARD_NO_AUTOINSTALL", "1")
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from spendguard.manage.token_operations import delegate_jobs, execution_journal  # noqa: E402


DELEGATE_REAP_RECOVERY_JOB_ID = "terminal-result-before-owner-death"
DELEGATE_REAP_RECOVERY_OWNER_CODE = "import sys; sys.stdin.read()"
DELEGATE_REAP_RECOVERY_OWNER_WAIT_S = 10
DELEGATE_REAP_RECOVERY_STALE_AGE_S = delegate_jobs.DELEGATE_JOB_DEFAULT_STALE_RUNNING_GRACE_S * 2
DELEGATE_REAP_RECOVERY_RESULT = "complete terminal result"
DELEGATE_REAP_RECOVERY_KIND = "agentic"
DELEGATE_REAP_RECOVERY_FINGERPRINT = "terminal-result-recovery"


def test_delegate_reap_recovers_terminal_result_before_reclaim():
    current = datetime.datetime.now(datetime.timezone.utc)
    stale_timestamp = current - datetime.timedelta(seconds=DELEGATE_REAP_RECOVERY_STALE_AGE_S)
    with subprocess.Popen(
            [sys.executable, "-c", DELEGATE_REAP_RECOVERY_OWNER_CODE], stdin=subprocess.PIPE) as owner:
        try:
            execution_id = execution_journal.begin_durable_execution(
                metadata={"owner_pid": owner.pid})
            execution_journal.terminalize_durable_execution(
                execution_id, "success", result=DELEGATE_REAP_RECOVERY_RESULT, exit_code=0)
        finally:
            owner.kill()
            owner.wait(timeout=DELEGATE_REAP_RECOVERY_OWNER_WAIT_S)

    with delegate_jobs._open_delegate_jobs_connection() as connection:
        connection.execute(
            "INSERT INTO delegate_jobs(job_id,created_ts,updated_ts,state,kind,task_fingerprint,cwd,"
            "provider,intent,dirty_before,execution_id,owning_host) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (DELEGATE_REAP_RECOVERY_JOB_ID, stale_timestamp.isoformat(), stale_timestamp.isoformat(),
             delegate_jobs.DELEGATE_JOB_RUNNING_STATE, DELEGATE_REAP_RECOVERY_KIND,
             DELEGATE_REAP_RECOVERY_FINGERPRINT, os.getcwd(), None, None, "[]", execution_id,
             delegate_jobs.socket.gethostname()))

    report = delegate_jobs.maintain_delegate_job_lifecycle(
        execute=True, reap_running=True, retain_payloads=False, now=current)
    recovered_row = delegate_jobs._delegate_job_row(DELEGATE_REAP_RECOVERY_JOB_ID)

    assert recovered_row["state"] == delegate_jobs.DELEGATE_JOB_DONE_STATE, {
        "row": dict(recovered_row), "report": report}
    assert recovered_row["state"] != delegate_jobs.DELEGATE_JOB_RECLAIMED_STATE
    assert json.loads(recovered_row["result_payload"]) == DELEGATE_REAP_RECOVERY_RESULT
    assert report["recovered"] == [DELEGATE_REAP_RECOVERY_JOB_ID]


test_delegate_reap_recovers_terminal_result_before_reclaim()
print("delegate reap terminal-result recovery passed")
