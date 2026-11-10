"""Guard-failure contracts for delegate-job lifecycle maintenance."""
import datetime, json, os, psutil, socket, subprocess, sys, tempfile
from unittest import mock

TEST_HOME = os.environ.get("SPENDGUARD_HOME") or tempfile.mkdtemp(prefix="sg-delegate-maintenance-")
os.environ.setdefault("SPENDGUARD_HOME", TEST_HOME)
os.environ.setdefault("SPENDGUARD_TEST_ISOLATED", "1")
os.environ.setdefault("SPENDGUARD_NO_AUTOINSTALL", "1")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))
from spendguard.manage.token_operations import delegate_jobs, execution_journal  # noqa: E402

STALE_GRACE_SECONDS = 60
ORPHAN_RUNNING_GRACE_SECONDS = 600
AGED_ORPHAN_AGE_SECONDS = ORPHAN_RUNNING_GRACE_SECONDS + STALE_GRACE_SECONDS
YOUNG_ORPHAN_AGE_SECONDS = STALE_GRACE_SECONDS * 2
PAYLOAD_RETENTION_DAYS = 7
MAINTENANCE_BATCH_SIZE = 100
TRUNCATION_TEST_BATCH_SIZE = 1
VACUUM_MIN_FREED_BYTES = 2 ** 62
OLD_AGE_DAYS = PAYLOAD_RETENTION_DAYS + 1
YOUNG_AGE_DAYS = PAYLOAD_RETENTION_DAYS - 1
CHILD_WAIT_SECONDS = 60
failures = []


def check_delegate_maintenance_contract(label, condition):
    print(f"  [{'OK' if condition else 'FAIL'}] {label}")
    if not condition:
        failures.append(label)


def insert_delegate_maintenance_fixture(job_id, state, updated_ts, payload, execution_id):
    timestamp = updated_ts.isoformat()
    with delegate_jobs._open_delegate_jobs_connection() as connection:
        connection.execute(
            "INSERT INTO delegate_jobs(job_id,created_ts,updated_ts,state,kind,task_fingerprint,cwd,provider,"
            "intent,dirty_before,result_payload,execution_id,owning_host) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (job_id, timestamp, timestamp, state, "agentic", f"fingerprint-{job_id}", os.getcwd(),
             "fixture-provider", "fixture-intent", "[]", json.dumps(payload) if payload is not None else None,
             execution_id, socket.gethostname()))


original_cfg_get = delegate_jobs.config._cfg_get
configured_values = {"stale_running_grace_s": STALE_GRACE_SECONDS,
                     "orphan_running_grace_s": ORPHAN_RUNNING_GRACE_SECONDS,
                     "payload_retention_days": PAYLOAD_RETENTION_DAYS,
                     "maintenance_batch_size": MAINTENANCE_BATCH_SIZE,
                     "vacuum_min_freed_bytes": VACUUM_MIN_FREED_BYTES}
configured_getter = lambda section, key, default=None: (
    configured_values[key] if section == "delegate_jobs" and key in configured_values
    else original_cfg_get(section, key, default))
current = datetime.datetime.now(datetime.timezone.utc)
old = current - datetime.timedelta(days=OLD_AGE_DAYS)
child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
child_birth = psutil.Process(child.pid).create_time()
child.terminate()
child.wait(timeout=CHILD_WAIT_SECONDS)
insert_delegate_maintenance_fixture("dead-stale-job", "running", old, None, "dead-execution")
insert_delegate_maintenance_fixture("live-stale-job", "running", old, None, "live-execution")
insert_delegate_maintenance_fixture(
    "aged-orphan-job", "running",
    current - datetime.timedelta(seconds=AGED_ORPHAN_AGE_SECONDS), None, None)
insert_delegate_maintenance_fixture(
    "young-orphan-job", "running",
    current - datetime.timedelta(seconds=YOUNG_ORPHAN_AGE_SECONDS), None, None)
identities = {"dead-execution": {"owner_pid": child.pid, "owner_birth": child_birth},
              "live-execution": {"owner_pid": os.getpid(),
                                 "owner_birth": psutil.Process(os.getpid()).create_time()}}
with mock.patch.object(delegate_jobs.config, "_cfg_get", configured_getter), mock.patch.object(
        execution_journal, "recover_durable_execution", side_effect=lambda execution_id: identities[execution_id]):
    reap_report = delegate_jobs.maintain_delegate_job_lifecycle(
        execute=True, reap_running=True, retain_payloads=False, now=current)
check_delegate_maintenance_contract("dead stale worker is reclaimed", "dead-stale-job" in reap_report["reclaimed"])
check_delegate_maintenance_contract("reclaimed state is auditable",
                                    delegate_jobs._delegate_job_row("dead-stale-job")["state"] == "reclaimed")
check_delegate_maintenance_contract("live stale worker is untouched",
                                    delegate_jobs._delegate_job_row("live-stale-job")["state"] == "running")
aged_orphan_row = delegate_jobs._delegate_job_row("aged-orphan-job")
check_delegate_maintenance_contract("aged orphan is reclaimed",
                                    aged_orphan_row["state"] == delegate_jobs.DELEGATE_JOB_RECLAIMED_STATE)
check_delegate_maintenance_contract("aged orphan has its distinct terminal reason",
                                    aged_orphan_row["terminal_reason"] == delegate_jobs.DELEGATE_JOB_ORPHAN_REASON)
check_delegate_maintenance_contract("young orphan is untouched",
                                    delegate_jobs._delegate_job_row("young-orphan-job")["state"]
                                    == delegate_jobs.DELEGATE_JOB_RUNNING_STATE)

old_payload = {"execution": {"text": "whole archived evidence"}, "tail": ["late", "evidence"]}
young_payload = {"execution": {"text": "young evidence remains hot"}}
insert_delegate_maintenance_fixture("old-terminal-job", "done", old, old_payload, None)
insert_delegate_maintenance_fixture("young-terminal-job", "done",
                                    current - datetime.timedelta(days=YOUNG_AGE_DAYS), young_payload, None)
with mock.patch.object(delegate_jobs.config, "_cfg_get", configured_getter):
    retention_report = delegate_jobs.maintain_delegate_job_lifecycle(
        execute=True, reap_running=False, retain_payloads=True, now=current)
archived_row = delegate_jobs._delegate_job_row("old-terminal-job")
young_row = delegate_jobs._delegate_job_row("young-terminal-job")
check_delegate_maintenance_contract("only expired terminal payload is archived",
                                    [item["job_id"] for item in retention_report["archived"]]
                                    == ["old-terminal-job"])
check_delegate_maintenance_contract("archive pointer is persisted", bool(archived_row["payload_archive_path"]))
check_delegate_maintenance_contract("whole archived payload remains retrievable",
                                    delegate_jobs.read_delegate_job_result("old-terminal-job")["tail"]
                                    == old_payload["tail"])
check_delegate_maintenance_contract("young terminal payload remains hot",
                                    json.loads(young_row["result_payload"]) == young_payload
                                    and young_row["payload_archive_path"] is None)

with delegate_jobs._open_delegate_jobs_connection() as connection:
    connection.execute("UPDATE delegate_jobs SET state=? WHERE state=?",
                       (delegate_jobs.DELEGATE_JOB_KILLED_STATE, delegate_jobs.DELEGATE_JOB_RUNNING_STATE))
configured_values["maintenance_batch_size"] = TRUNCATION_TEST_BATCH_SIZE
insert_delegate_maintenance_fixture("first-dead-stale-job", "running", old, None, "first-dead-execution")
insert_delegate_maintenance_fixture("second-dead-stale-job", "running", old, None, "second-dead-execution")
dead_identities = {
    "first-dead-execution": {"owner_pid": child.pid, "owner_birth": child_birth},
    "second-dead-execution": {"owner_pid": child.pid, "owner_birth": child_birth},
}
with mock.patch.object(delegate_jobs.config, "_cfg_get", configured_getter), mock.patch.object(
        execution_journal, "recover_durable_execution",
        side_effect=lambda execution_id: dead_identities[execution_id]):
    capped_reap_report = delegate_jobs.maintain_delegate_job_lifecycle(
        execute=True, reap_running=True, retain_payloads=False, now=current)
check_delegate_maintenance_contract("capped reaper reclaims exactly one row",
                                    len(capped_reap_report["reclaimed"]) == TRUNCATION_TEST_BATCH_SIZE)
check_delegate_maintenance_contract("capped reaper reports its limit",
                                    capped_reap_report["reap_limit_reached"] is True)
check_delegate_maintenance_contract("capped reaper reports the eligible denominator",
                                    capped_reap_report["reap_eligible_total"] == len(dead_identities))
configured_values["maintenance_batch_size"] = MAINTENANCE_BATCH_SIZE
with mock.patch.object(delegate_jobs.config, "_cfg_get", configured_getter), mock.patch.object(
        execution_journal, "recover_durable_execution",
        side_effect=lambda execution_id: dead_identities[execution_id]):
    uncapped_reap_report = delegate_jobs.maintain_delegate_job_lifecycle(
        execute=True, reap_running=True, retain_payloads=False, now=current)
check_delegate_maintenance_contract("uncapped reaper does not report its limit",
                                    uncapped_reap_report["reap_limit_reached"] is False)

with delegate_jobs._open_delegate_jobs_connection() as connection:
    connection.execute("UPDATE delegate_jobs SET state=? WHERE state=?",
                       (delegate_jobs.DELEGATE_JOB_KILLED_STATE, delegate_jobs.DELEGATE_JOB_RUNNING_STATE))
configured_values["maintenance_batch_size"] = TRUNCATION_TEST_BATCH_SIZE
insert_delegate_maintenance_fixture("first-expired-payload-job", "done", old, old_payload, None)
insert_delegate_maintenance_fixture("second-expired-payload-job", "done", old, old_payload, None)
with mock.patch.object(delegate_jobs.config, "_cfg_get", configured_getter):
    capped_archive_report = delegate_jobs.maintain_delegate_job_lifecycle(
        execute=True, reap_running=False, retain_payloads=True, now=current)
check_delegate_maintenance_contract("capped archival archives exactly one row",
                                    len(capped_archive_report["archived"]) == TRUNCATION_TEST_BATCH_SIZE)
check_delegate_maintenance_contract("capped archival reports its limit",
                                    capped_archive_report["archive_limit_reached"] is True)
check_delegate_maintenance_contract("capped archival reports one remaining row",
                                    capped_archive_report["archive_remaining"] == TRUNCATION_TEST_BATCH_SIZE)
configured_values["maintenance_batch_size"] = MAINTENANCE_BATCH_SIZE
with mock.patch.object(delegate_jobs.config, "_cfg_get", configured_getter):
    uncapped_archive_report = delegate_jobs.maintain_delegate_job_lifecycle(
        execute=True, reap_running=False, retain_payloads=True, now=current)
check_delegate_maintenance_contract("uncapped archival does not report its limit",
                                    uncapped_archive_report["archive_limit_reached"] is False)
check_delegate_maintenance_contract("uncapped archival reports no remaining rows",
                                    uncapped_archive_report["archive_remaining"] == 0)
if failures:
    raise SystemExit(f"{len(failures)} delegate maintenance contract(s) failed: {failures}")
print("delegate-job lifecycle maintenance contracts passed")
