"""Durable lifecycle storage for detached agentic delegate executions.

This follows ``lane_queue._ensure_queue_schema`` and ``queue_row_status`` for
SQLite/WAL and read-only status access, but deliberately uses a separate table
and lifecycle: delegate jobs are completed by their own executor threads, not
leased to lane workers.
"""
import datetime
import contextlib
import json
import os
import sqlite3
import sys
import uuid
from pathlib import Path

from . import config

DELEGATE_JOBS_DATABASE_FILENAME = "delegate_jobs.db"
DELEGATE_JOB_RUNNING_STATE = "running"
DELEGATE_JOB_DONE_STATE = "done"
DELEGATE_JOB_FAILED_STATE = "failed"
DELEGATE_JOB_LOST_STATE = "lost"
DELEGATE_JOB_KILLED_STATE = "killed"
DELEGATE_JOB_TERMINAL_STATES = (DELEGATE_JOB_DONE_STATE, DELEGATE_JOB_FAILED_STATE,
                                DELEGATE_JOB_LOST_STATE, DELEGATE_JOB_KILLED_STATE)
DELEGATE_JOB_JOURNAL_STATES = {"done": "success", "failed": "failure", "lost": "lost", "killed": "killed"}
DELEGATE_JOB_RESTART_REASON = "server-restart-unknown"
DELEGATE_JOB_STORE_UNREADABLE_REASON = "store-unreadable"
DELEGATE_JOB_DEFAULT_RETENTION_S = 604800
DELEGATE_JOB_DEFAULT_STALENESS_MARGIN_S = 60
DELEGATE_JOB_DEFAULT_CODEX_TIMEOUT_S = 1800
DELEGATE_JOBS_RETENTION_S = float(config._cfg_get(
    "delegate", "jobs_retention_s", DELEGATE_JOB_DEFAULT_RETENTION_S) or DELEGATE_JOB_DEFAULT_RETENTION_S)
DELEGATE_JOB_STALENESS_MARGIN_S = float(config._cfg_get(
    "delegate", "job_staleness_margin_s", DELEGATE_JOB_DEFAULT_STALENESS_MARGIN_S)
    or DELEGATE_JOB_DEFAULT_STALENESS_MARGIN_S)


class DelegateJobStoreUnreadable(Exception):
    """The delegate-jobs store exists but a read failed; absence is unknown."""


def delegate_jobs_database_path():
    """Resolve the configured delegate-job database under spendguard HOME."""
    configured = config._cfg_get("delegate", "jobs_db_path", None)
    path = Path(configured).expanduser() if configured else config.HOME / DELEGATE_JOBS_DATABASE_FILENAME
    return path if path.is_absolute() else config.HOME / path


def _ensure_delegate_jobs_schema(connection):
    connection.execute("""CREATE TABLE IF NOT EXISTS delegate_jobs(
        job_id TEXT PRIMARY KEY, created_ts TEXT NOT NULL, updated_ts TEXT NOT NULL,
        state TEXT NOT NULL, kind TEXT NOT NULL, task_fingerprint TEXT NOT NULL,
        cwd TEXT NOT NULL, provider TEXT, intent TEXT, dirty_before TEXT NOT NULL,
        result_payload TEXT, terminal_reason TEXT, error TEXT)""")
    connection.execute("CREATE INDEX IF NOT EXISTS delegate_jobs_inflight "
                       "ON delegate_jobs(task_fingerprint,cwd,state,created_ts)")
    connection.execute("CREATE INDEX IF NOT EXISTS delegate_jobs_retention "
                       "ON delegate_jobs(state,updated_ts)")
    columns = {row[1] for row in connection.execute("PRAGMA table_info(delegate_jobs)")}
    if "execution_id" not in columns:
        connection.execute("ALTER TABLE delegate_jobs ADD COLUMN execution_id TEXT")


@contextlib.contextmanager
def _open_delegate_jobs_connection():
    try:
        path = delegate_jobs_database_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with contextlib.closing(config.fresh_ledger_conn(_ensure_delegate_jobs_schema, path)) as connection:
            with connection:
                yield connection
    except (sqlite3.Error, OSError) as error:
        from .execution_journal import DurableExecutionPersistenceError
        raise DurableExecutionPersistenceError("delegate job lifecycle persistence failed") from error


def _open_delegate_jobs_readonly_connection():
    uri = delegate_jobs_database_path().expanduser().resolve().as_uri() + "?mode=ro"
    return config.managed_ledger_read_connection(uri, uri=True)


def _delegate_job_timestamp():
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


def create_delegate_job(*, kind, task_fingerprint, cwd, provider, intent, dirty_before, owner_pid=None,
                        model=None):
    """Insert one running delegate job and immediately return its UUID4 hex id."""
    prune_delegate_jobs()
    job_id = uuid.uuid4().hex
    from .execution_journal import begin_durable_execution
    from .delegate_router import codex_agentic_events_path, CODEX_AGENTIC_EVENTS_SCHEMA_VERSION
    metadata = {"job_id": job_id, "jobs_database_path": os.fspath(delegate_jobs_database_path()),
                "kind": kind, "cwd": os.fspath(cwd), "provider": provider,
                "plan": provider, "model": model, "ledger_path": os.fspath(codex_agentic_events_path()),
                "schema_version": CODEX_AGENTIC_EVENTS_SCHEMA_VERSION}
    if owner_pid is not None:
        metadata["owner_pid"] = owner_pid
    execution_id = begin_durable_execution(metadata=metadata)
    now = _delegate_job_timestamp()
    with _open_delegate_jobs_connection() as connection:
        connection.execute(
            "INSERT INTO delegate_jobs(job_id,created_ts,updated_ts,state,kind,task_fingerprint,cwd,provider,"
            "intent,dirty_before,execution_id) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (job_id, now, now, DELEGATE_JOB_RUNNING_STATE, kind, task_fingerprint, os.fspath(cwd), provider,
             intent, json.dumps(sorted(dirty_before)), execution_id))
    return job_id


def finish_delegate_job(job_id, *, state, result_payload=None, terminal_reason=None, error=None):
    """Persist a terminal job result in full using an executor-owned connection."""
    if state not in DELEGATE_JOB_TERMINAL_STATES:
        raise ValueError(f"delegate job terminal state required, got {state!r}")
    payload = json.dumps(result_payload) if result_payload is not None else None
    row = _delegate_job_row(job_id)
    if row is None:
        raise KeyError(job_id)
    if row["execution_id"]:
        from .execution_journal import terminalize_durable_execution
        terminal = terminalize_durable_execution(row["execution_id"], DELEGATE_JOB_JOURNAL_STATES[state])
        winner_state = next(job_state for job_state, journal_state in DELEGATE_JOB_JOURNAL_STATES.items()
                            if journal_state == terminal["state"])
        if winner_state != state:
            payload = None
            terminal_reason = None
            error = None
        state = winner_state
    with _open_delegate_jobs_connection() as connection:
        connection.execute(
            "UPDATE delegate_jobs SET state=?,updated_ts=?,result_payload=?,terminal_reason=?,error=? "
            "WHERE job_id=? AND (state=? OR (state=? AND ?!=?))",
            (state, _delegate_job_timestamp(), payload, terminal_reason, error, job_id, DELEGATE_JOB_RUNNING_STATE,
             DELEGATE_JOB_LOST_STATE, state, DELEGATE_JOB_LOST_STATE))


def recover_delegate_job_execution(job_id):
    """Recover complete captured transport evidence and reconcile a dead executor."""
    from .execution_journal import recover_durable_execution
    row = _delegate_job_row(job_id)
    if row is None:
        return {"error": "not-found", "job_id": job_id}
    recovered = recover_durable_execution(row["execution_id"]) if row["execution_id"] else {}
    reverse_states = {value: key for key, value in DELEGATE_JOB_JOURNAL_STATES.items()}
    terminal_state = reverse_states.get(recovered.get("state"))
    owner_alive = True
    if recovered.get("owner_pid"):
        import psutil
        try:
            owner = psutil.Process(recovered["owner_pid"])
            owner_alive = (owner.create_time() == recovered.get("owner_birth")
                           and owner.status() != psutil.STATUS_ZOMBIE)
        except psutil.NoSuchProcess:
            owner_alive = False
    if (row["state"] in (DELEGATE_JOB_RUNNING_STATE, DELEGATE_JOB_LOST_STATE)
            and terminal_state
            and (not owner_alive or terminal_state in (DELEGATE_JOB_LOST_STATE, DELEGATE_JOB_KILLED_STATE))):
        finish_delegate_job(job_id, state=terminal_state, result_payload=recovered.get("result"),
                            terminal_reason=recovered.get("terminal_reason"))
        row = _delegate_job_row(job_id)
    return {**recovered, "job_id": job_id, "state": row["state"]}


def _delegate_job_row(job_id):
    try:
        with _open_delegate_jobs_readonly_connection() as connection:
            connection.row_factory = sqlite3.Row
            return connection.execute("SELECT * FROM delegate_jobs WHERE job_id=?", (job_id,)).fetchone()
    except sqlite3.OperationalError as error:
        if not delegate_jobs_database_path().exists():
            return None
        raise DelegateJobStoreUnreadable(job_id) from error


def _delegate_running_is_stale(updated_ts):
    """Return whether a running job predates the configured timeout and margin."""
    updated = datetime.datetime.fromisoformat(updated_ts)
    timeout_s = float(config._cfg_get(
        "delegate", "agentic_timeout_s", DELEGATE_JOB_DEFAULT_CODEX_TIMEOUT_S)
        or DELEGATE_JOB_DEFAULT_CODEX_TIMEOUT_S)
    stale_after = datetime.timedelta(seconds=timeout_s + DELEGATE_JOB_STALENESS_MARGIN_S)
    return datetime.datetime.now(datetime.timezone.utc) - updated > stale_after


def read_delegate_job(job_id):
    """Read job status; stale running jobs are reported failed without mutating history."""
    try:
        row = _delegate_job_row(job_id)
    except DelegateJobStoreUnreadable:
        print(f"[spendguard] delegate_jobs.read_delegate_job: store unreadable while looking for job "
              f"{job_id}; state is indeterminate", file=sys.stderr, flush=True)
        return {"state": "indeterminate", "job_id": job_id,
                "terminal_reason": DELEGATE_JOB_STORE_UNREADABLE_REASON}
    if row is None:
        return {"error": "not-found", "job_id": job_id}
    if row["state"] == DELEGATE_JOB_RUNNING_STATE and row["execution_id"]:
        recover_delegate_job_execution(job_id)
        row = _delegate_job_row(job_id)
    result = dict(row)
    result.pop("result_payload", None)
    result["dirty_before"] = json.loads(result["dirty_before"])
    if (result["state"] == DELEGATE_JOB_RUNNING_STATE
            and _delegate_running_is_stale(result["updated_ts"])):
        result["state"] = DELEGATE_JOB_FAILED_STATE
        result["terminal_reason"] = DELEGATE_JOB_RESTART_REASON
    return result


def read_delegate_job_result(job_id):
    """Return the complete authoritative terminal payload for one job."""
    try:
        row = _delegate_job_row(job_id)
    except DelegateJobStoreUnreadable:
        print(f"[spendguard] delegate_jobs.read_delegate_job_result: store unreadable while looking for job "
              f"{job_id}; status is indeterminate", file=sys.stderr, flush=True)
        return {"status": "indeterminate", "job_id": job_id,
                "terminal_reason": DELEGATE_JOB_STORE_UNREADABLE_REASON}
    if row is None:
        return {"error": "not-found", "job_id": job_id}
    if row["state"] == DELEGATE_JOB_RUNNING_STATE and row["execution_id"]:
        recover_delegate_job_execution(job_id)
        row = _delegate_job_row(job_id)
    if row["state"] == DELEGATE_JOB_RUNNING_STATE:
        if _delegate_running_is_stale(row["updated_ts"]):
            return {"status": DELEGATE_JOB_FAILED_STATE, "job_id": job_id,
                    "terminal_reason": DELEGATE_JOB_RESTART_REASON}
        return {"status": DELEGATE_JOB_RUNNING_STATE, "job_id": job_id,
                "terminal_reason": None}
    if row["result_payload"] is not None:
        return json.loads(row["result_payload"])
    return {"status": row["state"], "job_id": job_id,
            "terminal_reason": row["terminal_reason"], "error": row["error"]}


def find_inflight_delegate_job(task_fingerprint, cwd):
    """Find the oldest identical running job for exactly-once submit behavior."""
    try:
        with _open_delegate_jobs_readonly_connection() as connection:
            row = connection.execute(
                "SELECT job_id FROM delegate_jobs WHERE task_fingerprint=? AND cwd=? AND state=? "
                "ORDER BY created_ts LIMIT 1",
                (task_fingerprint, os.fspath(cwd), DELEGATE_JOB_RUNNING_STATE)).fetchone()
        if row:
            status = read_delegate_job(row[0])
            if status.get("state") == "indeterminate":
                raise DelegateJobStoreUnreadable(row[0])
            if status.get("state") == DELEGATE_JOB_RUNNING_STATE:
                return row[0]
    except sqlite3.OperationalError as error:
        if not delegate_jobs_database_path().exists():
            return None
        raise DelegateJobStoreUnreadable(task_fingerprint) from error
    return None


def prune_delegate_jobs(retention_s=DELEGATE_JOBS_RETENTION_S):
    """Delete terminal job rows older than the configured retention window."""
    cutoff = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(seconds=float(retention_s))
    try:
        with _open_delegate_jobs_connection() as connection:
            placeholders = ",".join("?" for _ in DELEGATE_JOB_TERMINAL_STATES)
            connection.execute(f"DELETE FROM delegate_jobs WHERE state IN ({placeholders}) AND updated_ts<?",
                               (*DELEGATE_JOB_TERMINAL_STATES, cutoff.isoformat(timespec="seconds")))
    except sqlite3.OperationalError:
        return
