"""Durable lifecycle storage for detached agentic delegate executions.

This follows ``lane_queue._ensure_queue_schema`` and ``queue_row_status`` for
SQLite/WAL and read-only status access, but deliberately uses a separate table
and lifecycle: each job belongs to an OS-detached worker, independently of its
launcher or MCP connection. The journal tracks the worker and provider process
identities; this store projects those durable outcomes for status and collection.

Workspace metadata is independent of the provider envelope. Cancellation or a
lost process can leave no completed provider result, but the retained worktree
must remain discoverable from both status and collection for inspection.
"""
import datetime
import contextlib
import gzip
import json
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import time
import uuid
from pathlib import Path

from ... import config
from .process_group_reaper import PROCESS_GROUP_LIVENESS_PROBE_SIGNAL, reap_process_group_bounded

DELEGATE_JOBS_DATABASE_FILENAME = "delegate_jobs.db"
DELEGATE_JOB_RUNNING_STATE = "running"
DELEGATE_JOB_DONE_STATE = "done"
DELEGATE_JOB_FAILED_STATE = "failed"
DELEGATE_JOB_LOST_STATE = "lost"
DELEGATE_JOB_KILLED_STATE = "killed"
DELEGATE_JOB_RECLAIMED_STATE = "reclaimed"
DELEGATE_JOB_TERMINAL_STATES = (DELEGATE_JOB_DONE_STATE, DELEGATE_JOB_FAILED_STATE,
                                DELEGATE_JOB_LOST_STATE, DELEGATE_JOB_KILLED_STATE,
                                DELEGATE_JOB_RECLAIMED_STATE)
DELEGATE_JOB_JOURNAL_STATES = {"done": "success", "failed": "failure", "lost": "lost", "killed": "killed"}
DELEGATE_JOB_RESTART_REASON = "server-restart-unknown"
DELEGATE_JOB_STORE_UNREADABLE_REASON = "store-unreadable"
DELEGATE_JOB_DEFAULT_RETENTION_S = 604800
DELEGATE_JOB_DEFAULT_STALENESS_MARGIN_S = 60
DELEGATE_JOB_DEFAULT_CODEX_TIMEOUT_S = 1800
DELEGATE_JOB_WAIT_POLL_DEFAULT_S = 0.1
DELEGATE_JOB_CANCEL_GRACE_DEFAULT_S = 5.0
DELEGATE_JOB_CANCEL_GROUP_POLL_DEFAULT_S = 0.05
DELEGATE_JOB_CANCEL_GROUP_WARNING_CONTEXT = "delegate cancel"
DELEGATE_JOB_CANCEL_REASON = "operator-cancelled"
DELEGATE_JOB_DEFAULT_STALE_RUNNING_GRACE_S = 3600
DELEGATE_JOB_DEFAULT_ORPHAN_RUNNING_GRACE_S = 86400
DELEGATE_JOB_DEFAULT_PAYLOAD_RETENTION_DAYS = 30
DELEGATE_JOB_DEFAULT_MAINTENANCE_BATCH_SIZE = 100
DELEGATE_JOB_DEFAULT_VACUUM_MIN_FREED_BYTES = 64 * 1024 * 1024
DELEGATE_JOB_ARCHIVE_DIRECTORY_NAME = "delegate-job-payloads"
DELEGATE_JOB_ARCHIVE_POINTER_KIND = "spendguard-delegate-payload-archive-v1"
DELEGATE_JOB_RECLAIM_REASON = "local-worker-provably-dead-after-stale-grace"
DELEGATE_JOB_ORPHAN_REASON = "orphan: running with no durable execution record, aged past orphan grace"
CODEX_DELETE_MISSING_SESSION_TEMPLATES = (
    "No active or archived session found matching '{thread_id}'.",
    "thread not found: {thread_id}",
)
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
    for column in ("codex_thread_id", "codex_session_reaped_at", "codex_session_reap_failed_at",
                   "codex_session_reap_failure_reason"):
        if column not in columns:
            connection.execute(f"ALTER TABLE delegate_jobs ADD COLUMN {column} TEXT")
    if "codex_session_reap_attempts" not in columns:
        connection.execute(
            "ALTER TABLE delegate_jobs ADD COLUMN codex_session_reap_attempts INTEGER NOT NULL DEFAULT 0")
    legacy_rows_need_host_attribution = "owning_host" not in columns
    for column in ("owning_host", "payload_archive_path"):
        if column not in columns:
            connection.execute(f"ALTER TABLE delegate_jobs ADD COLUMN {column} TEXT")
    if legacy_rows_need_host_attribution:
        # This database and its execution journal are host-local. Attribute rows
        # created before host provenance was persisted to the migrating host.
        connection.execute(
            "UPDATE delegate_jobs SET owning_host=? WHERE owning_host IS NULL",
            (socket.gethostname(),),
        )
    if "codex_thread_id" not in columns:
        for job_id, payload in connection.execute(
                "SELECT job_id,result_payload FROM delegate_jobs WHERE result_payload IS NOT NULL").fetchall():
            result = json.loads(payload)
            execution = result.get("execution", result) if isinstance(result, dict) else {}
            thread_id = execution.get("thread") if isinstance(execution, dict) else None
            if thread_id:
                connection.execute("UPDATE delegate_jobs SET codex_thread_id=? WHERE job_id=?", (thread_id, job_id))
    connection.execute("CREATE INDEX IF NOT EXISTS delegate_jobs_codex_retention "
                       "ON delegate_jobs(codex_session_reaped_at,state,updated_ts,codex_thread_id)")
    connection.execute("CREATE INDEX IF NOT EXISTS delegate_jobs_codex_thread "
                       "ON delegate_jobs(codex_thread_id)")
    connection.execute("""CREATE TABLE IF NOT EXISTS delegate_codex_session_leases(
        thread_id TEXT NOT NULL, worker_pid INTEGER NOT NULL, worker_birth REAL NOT NULL,
        active INTEGER NOT NULL, PRIMARY KEY(thread_id,worker_pid,worker_birth))""")


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
    try:
        from .delegate_worktrees import reclaim_expired_delegate_worktrees
        from .execution_journal import configured_delegate_worktree_reclaim_dependencies
        reclaim_expired_delegate_worktrees(**configured_delegate_worktree_reclaim_dependencies())
    except Exception as error:
        config.warn_once(f"[spendguard] delegate worktree sweep failed: {error}")
    job_id = uuid.uuid4().hex
    from .execution_journal import begin_durable_execution
    from . import token_operations_accessors
    metadata = {"job_id": job_id, "jobs_database_path": os.fspath(delegate_jobs_database_path()),
                "kind": kind, "cwd": os.fspath(cwd), "provider": provider,
                "plan": provider, "model": model,
                "ledger_path": os.fspath(token_operations_accessors.resolve_codex_agentic_events_path()),
                "schema_version": token_operations_accessors.resolve_CODEX_AGENTIC_EVENTS_SCHEMA_VERSION()}
    if owner_pid is not None:
        metadata["owner_pid"] = owner_pid
    execution_id = begin_durable_execution(metadata=metadata)
    now = _delegate_job_timestamp()
    with _open_delegate_jobs_connection() as connection:
        connection.execute(
            "INSERT INTO delegate_jobs(job_id,created_ts,updated_ts,state,kind,task_fingerprint,cwd,provider,"
            "intent,dirty_before,execution_id,owning_host) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (job_id, now, now, DELEGATE_JOB_RUNNING_STATE, kind, task_fingerprint, os.fspath(cwd), provider,
             intent, json.dumps(sorted(dirty_before)), execution_id, socket.gethostname()))
    return job_id


def finish_delegate_job(job_id, *, state, result_payload=None, terminal_reason=None, error=None):
    """Persist a terminal job result in full using an executor-owned connection."""
    if state not in DELEGATE_JOB_TERMINAL_STATES:
        raise ValueError(f"delegate job terminal state required, got {state!r}")
    payload = json.dumps(result_payload) if result_payload is not None else None
    execution = result_payload.get("execution", result_payload) if isinstance(result_payload, dict) else {}
    codex_thread_id = execution.get("thread") if isinstance(execution, dict) else None
    row = _delegate_job_row(job_id)
    if row is None:
        raise KeyError(job_id)
    if codex_thread_id is None and row["provider"] == "codex" and row["execution_id"]:
        from .execution_journal import parse_execution_codex_thread
        from .execution_journal import durable_execution_payload_paths
        stdout = durable_execution_payload_paths(row["execution_id"])["stdout"]
        if stdout.exists():
            codex_thread_id = parse_execution_codex_thread(stdout.read_text(encoding="utf-8"))
    if row["execution_id"]:
        from .execution_journal import terminalize_durable_execution
        terminal = terminalize_durable_execution(
            row["execution_id"], DELEGATE_JOB_JOURNAL_STATES[state],
            terminal_recovery_result=result_payload)
        winner_state = next(job_state for job_state, journal_state in DELEGATE_JOB_JOURNAL_STATES.items()
                            if journal_state == terminal["state"])
        if winner_state != state:
            payload = None
            terminal_reason = None
            error = None
        state = winner_state
    with _open_delegate_jobs_connection() as connection:
        if codex_thread_id:
            connection.execute("UPDATE delegate_jobs SET codex_thread_id=COALESCE(codex_thread_id,?) "
                               "WHERE job_id=?", (codex_thread_id, job_id))
        connection.execute(
            "UPDATE delegate_jobs SET state=?,updated_ts=?,result_payload=?,terminal_reason=?,error=?,"
            "codex_thread_id=COALESCE(?,codex_thread_id) "
            "WHERE job_id=? AND (state=? OR (state=? AND ?!=?))",
            (state, _delegate_job_timestamp(), payload, terminal_reason, error, codex_thread_id,
             job_id, DELEGATE_JOB_RUNNING_STATE,
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


def _delegate_job_workspace_details(row):
    from .delegate_worktrees import read_delegate_worktree_details
    from .execution_journal import durable_execution_payload_paths
    return read_delegate_worktree_details(durable_execution_payload_paths(row["execution_id"])["envelope"]) if row["execution_id"] else {}


def read_delegate_job(job_id):
    """Read job status; stale running jobs are reported failed without mutating history."""
    try:
        row = _delegate_job_row(job_id)
    except DelegateJobStoreUnreadable:
        print(f"[spendguard] delegate_jobs.read_delegate_job: store unreadable while looking for job "
              f"{job_id}; state is indeterminate", file=sys.stderr, flush=True)
        return {"state": "indeterminate", "status": "indeterminate", "job_id": job_id,
                "terminal_reason": DELEGATE_JOB_STORE_UNREADABLE_REASON}
    if row is None:
        return {"error": "not-found", "job_id": job_id}
    if row["state"] == DELEGATE_JOB_RUNNING_STATE and row["execution_id"]:
        recover_delegate_job_execution(job_id)
        row = _delegate_job_row(job_id)
    result = dict(row)
    result.update(_delegate_job_workspace_details(row))
    result.pop("result_payload", None)
    result["dirty_before"] = json.loads(result["dirty_before"])
    if (result["state"] == DELEGATE_JOB_RUNNING_STATE
            and _delegate_running_is_stale(result["updated_ts"])):
        result["state"] = DELEGATE_JOB_FAILED_STATE
        result["terminal_reason"] = DELEGATE_JOB_RESTART_REASON
    result["status"] = result["state"]
    return result


def read_delegate_job_result(job_id):
    """Return the complete authoritative terminal payload for one job."""
    try:
        row = _delegate_job_row(job_id)
    except DelegateJobStoreUnreadable:
        print(f"[spendguard] delegate_jobs.read_delegate_job_result: store unreadable while looking for job "
              f"{job_id}; status is indeterminate", file=sys.stderr, flush=True)
        return {"state": "indeterminate", "status": "indeterminate", "job_id": job_id,
                "terminal_reason": DELEGATE_JOB_STORE_UNREADABLE_REASON}
    if row is None:
        return {"error": "not-found", "job_id": job_id}
    if row["state"] == DELEGATE_JOB_RUNNING_STATE and row["execution_id"]:
        recover_delegate_job_execution(job_id)
        row = _delegate_job_row(job_id)
    if row["state"] == DELEGATE_JOB_RUNNING_STATE:
        if _delegate_running_is_stale(row["updated_ts"]):
            return {"state": DELEGATE_JOB_FAILED_STATE, "status": DELEGATE_JOB_FAILED_STATE, "job_id": job_id,
                    "terminal_reason": DELEGATE_JOB_RESTART_REASON}
        return {"state": DELEGATE_JOB_RUNNING_STATE, "status": DELEGATE_JOB_RUNNING_STATE, "job_id": job_id,
                "terminal_reason": None}
    if row["result_payload"] is not None:
        payload = json.loads(row["result_payload"])
        if (isinstance(payload, dict)
                and payload.get("kind") == DELEGATE_JOB_ARCHIVE_POINTER_KIND):
            with gzip.open(Path(payload["path"]), "rt", encoding="utf-8") as archive_stream:
                payload = json.load(archive_stream)
        if isinstance(payload, dict):
            return {**payload, **_delegate_job_workspace_details(row), "state": row["state"]}
        return {"state": row["state"], "status": row["state"], "job_id": job_id,
                "terminal_reason": row["terminal_reason"], "error": row["error"],
                "result": payload, **_delegate_job_workspace_details(row)}
    return {"state": row["state"], "status": row["state"], "job_id": job_id,
            "terminal_reason": row["terminal_reason"], "error": row["error"],
            **_delegate_job_workspace_details(row)}


def wait_for_delegate_job(job_id, timeout=None):
    """Block on the durable terminal signal, returning a running handle on deadline.

    The database and journal are the notification channel across process exits;
    no caller-owned thread or pipe is needed to observe completion.
    """
    if timeout is not None and timeout < 0:
        raise ValueError("delegate wait timeout must be nonnegative")
    deadline = None if timeout is None else time.monotonic() + timeout
    interval = float(config._cfg_get("delegate", "wait_poll_s", DELEGATE_JOB_WAIT_POLL_DEFAULT_S))
    if interval <= 0:
        raise ValueError("delegate.wait_poll_s must be positive")
    while True:
        status = read_delegate_job(job_id)
        if status.get("state") != DELEGATE_JOB_RUNNING_STATE:
            return read_delegate_job_result(job_id) if status.get("state") in DELEGATE_JOB_TERMINAL_STATES else status
        remaining = None if deadline is None else deadline - time.monotonic()
        if remaining is not None and remaining <= 0:
            return {**read_delegate_job_result(job_id), "wait_timed_out": True}
        time.sleep(interval if remaining is None else min(interval, remaining))


def cancel_delegate_job(job_id, force=False):
    """Cancel only identity-verified processes owned by this governed dispatch.

    Claim the journal terminal before signalling so executor/observer races cannot
    overwrite an operator cancellation. A shared MCP host is never signalled.
    """
    import psutil
    from . import execution_journal
    status = read_delegate_job(job_id)
    if status.get("state") != DELEGATE_JOB_RUNNING_STATE:
        return read_delegate_job_result(job_id) if status.get("state") in DELEGATE_JOB_TERMINAL_STATES else status
    execution_id = status.get("execution_id")
    if not execution_id:
        return {**status, "error": "delegate cancellation requires durable process identity"}
    execution = execution_journal.recover_durable_execution(execution_id)
    if execution["state"] != DELEGATE_JOB_RUNNING_STATE:
        return read_delegate_job_result(job_id)
    worker_pid, worker_birth = execution.get("worker_pid"), execution.get("worker_birth")
    worker_process_group = None
    worker_group_proven_empty = False
    if worker_pid is not None:
        try:
            worker = psutil.Process(worker_pid)
            if worker.create_time() != worker_birth:
                config.warn_once(
                    f"[spendguard] delegate cancel: refusing to signal worker {worker_pid} because its process "
                    "identity did not match the governed worker")
                return {**status, "error": "delegate termination unconfirmed"}
            if worker.status() != psutil.STATUS_ZOMBIE:
                observed_process_group = os.getpgid(worker_pid)
                if observed_process_group == worker_pid:
                    worker_process_group = observed_process_group
                else:
                    config.warn_once(
                        f"[spendguard] delegate cancel: refusing to signal worker {worker_pid} because its process "
                        f"group is {observed_process_group}, not its owned session group")
                    return {**status, "error": "delegate termination unconfirmed"}
            else:
                raise psutil.NoSuchProcess(worker_pid)
        except (psutil.NoSuchProcess, ProcessLookupError):
            try:
                os.killpg(worker_pid, PROCESS_GROUP_LIVENESS_PROBE_SIGNAL)
                worker_process_group = worker_pid
            except ProcessLookupError:
                worker_group_proven_empty = True
            except PermissionError as error:
                config.warn_once(
                    f"[spendguard] delegate cancel: cannot confirm orphaned process group {worker_pid} is empty "
                    f"because its liveness probe was denied: {error}")
                return {**status, "error": "delegate termination unconfirmed"}
            except OSError as error:
                return {**status, "error": f"delegate worker identity verification failed: {error}"}
        except (psutil.Error, OSError) as error:
            return {**status, "error": f"delegate worker identity verification failed: {error}"}
    if worker_process_group is None and not worker_group_proven_empty:
        return {**status, "error": "delegate process identity is not yet available; retry cancellation"}
    terminal = execution_journal.terminalize_durable_execution(execution_id, DELEGATE_JOB_KILLED_STATE)
    if terminal["state"] != DELEGATE_JOB_KILLED_STATE:
        return read_delegate_job_result(job_id)
    grace = float(config._cfg_get("delegate", "cancel_grace_s", DELEGATE_JOB_CANCEL_GRACE_DEFAULT_S))
    poll_interval = float(config._cfg_get(
        "delegate", "cancel_group_poll_s", DELEGATE_JOB_CANCEL_GROUP_POLL_DEFAULT_S))
    group_reaped = worker_group_proven_empty or reap_process_group_bounded(
        worker_process_group, terminate_grace_s=grace, force_kill_grace_s=grace,
        poll_interval_s=poll_interval, warning_context=DELEGATE_JOB_CANCEL_GROUP_WARNING_CONTEXT,
        emit_warning=config.warn_once,
        force_immediately=force)
    if not group_reaped:
        return {**status, "error": "delegate termination unconfirmed"}
    finish_delegate_job(job_id, state=DELEGATE_JOB_KILLED_STATE, terminal_reason=DELEGATE_JOB_CANCEL_REASON)
    return read_delegate_job_result(job_id)


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
    """Compatibility no-op: identity rows are permanent; maintenance archives payloads."""
    return {"identity_rows_deleted": 0, "retention_s": retention_s}


def _delegate_payload_archive_directory():
    configured = config._cfg_get("delegate_jobs", "payload_archive_dir", None)
    directory = Path(configured).expanduser() if configured else config.HOME / DELEGATE_JOB_ARCHIVE_DIRECTORY_NAME
    return directory if directory.is_absolute() else config.HOME / directory


def _delegate_jobs_maintenance_settings():
    settings = {
        "stale_running_grace_s": float(config._cfg_get(
            "delegate_jobs", "stale_running_grace_s", DELEGATE_JOB_DEFAULT_STALE_RUNNING_GRACE_S)),
        "orphan_running_grace_s": float(config._cfg_get(
            "delegate_jobs", "orphan_running_grace_s", DELEGATE_JOB_DEFAULT_ORPHAN_RUNNING_GRACE_S)),
        "payload_retention_days": float(config._cfg_get(
            "delegate_jobs", "payload_retention_days", DELEGATE_JOB_DEFAULT_PAYLOAD_RETENTION_DAYS)),
        "batch_size": int(config._cfg_get(
            "delegate_jobs", "maintenance_batch_size", DELEGATE_JOB_DEFAULT_MAINTENANCE_BATCH_SIZE)),
        "vacuum_min_freed_bytes": int(config._cfg_get(
            "delegate_jobs", "vacuum_min_freed_bytes", DELEGATE_JOB_DEFAULT_VACUUM_MIN_FREED_BYTES)),
    }
    if any(value < 0 for value in settings.values()) or settings["batch_size"] < 1:
        raise ValueError("delegate_jobs maintenance settings must be nonnegative and batch size positive")
    return settings


def maintain_delegate_job_lifecycle(*, execute=False, reap_running=True, retain_payloads=True, now=None):
    """Plan or apply bounded reaping and lossless payload archiving for delegate jobs."""
    from .execution_journal import recover_durable_execution
    from .warm_stdio_daemon import _warm_owner_identity_state, WARM_OWNER_IDENTITY_DEAD

    current = now or datetime.datetime.now(datetime.timezone.utc)
    settings = _delegate_jobs_maintenance_settings()
    report = {"mode": "execute" if execute else "dry-run", "reclaimed": [], "archived": [],
              "skipped": [], "errors": [], "checkpoint": False, "vacuum": "not-needed",
              "reclaimable_payload_bytes": 0, "reap_eligible_total": 0,
              "reap_limit_reached": False, "archive_eligible_total": 0,
              "archive_limit_reached": False, "archive_remaining": 0,
              "more_work_remaining": False}
    with _open_delegate_jobs_connection() as connection:
        connection.row_factory = sqlite3.Row
        if reap_running:
            cutoff = (current - datetime.timedelta(seconds=settings["stale_running_grace_s"])).isoformat()
            orphan_cutoff = (current - datetime.timedelta(
                seconds=settings["orphan_running_grace_s"])).isoformat()
            report["reap_eligible_total"] = connection.execute(
                "SELECT COUNT(*) FROM delegate_jobs WHERE state=? AND updated_ts<?",
                (DELEGATE_JOB_RUNNING_STATE, cutoff)).fetchone()[0]
            candidates = connection.execute(
                "SELECT * FROM delegate_jobs WHERE state=? AND updated_ts<? ORDER BY updated_ts LIMIT ?",
                (DELEGATE_JOB_RUNNING_STATE, cutoff, settings["batch_size"])).fetchall()
            report["reap_limit_reached"] = len(candidates) == settings["batch_size"]
            for row in candidates:
                if row["owning_host"] != socket.gethostname():
                    report["skipped"].append(row["job_id"])
                    continue
                if not row["execution_id"]:
                    if row["updated_ts"] >= orphan_cutoff:
                        report["skipped"].append(row["job_id"])
                        continue
                    report["reclaimed"].append(row["job_id"])
                    if execute:
                        connection.execute(
                            "UPDATE delegate_jobs SET state=?,updated_ts=?,terminal_reason=? "
                            "WHERE job_id=? AND state=?",
                            (DELEGATE_JOB_RECLAIMED_STATE, current.isoformat(), DELEGATE_JOB_ORPHAN_REASON,
                             row["job_id"], DELEGATE_JOB_RUNNING_STATE))
                    continue
                try:
                    execution = recover_durable_execution(row["execution_id"])
                    identity_state = _warm_owner_identity_state(
                        execution.get("owner_pid"), execution.get("owner_birth"))
                except Exception as error:
                    report["errors"].append(f"{row['job_id']}: liveness unavailable: {error}")
                    continue
                if identity_state != WARM_OWNER_IDENTITY_DEAD:
                    report["skipped"].append(row["job_id"])
                    continue
                report["reclaimed"].append(row["job_id"])
                if execute:
                    connection.execute(
                        "UPDATE delegate_jobs SET state=?,updated_ts=?,terminal_reason=? "
                        "WHERE job_id=? AND state=?",
                        (DELEGATE_JOB_RECLAIMED_STATE, current.isoformat(), DELEGATE_JOB_RECLAIM_REASON,
                         row["job_id"], DELEGATE_JOB_RUNNING_STATE))
        if retain_payloads:
            cutoff = (current - datetime.timedelta(days=settings["payload_retention_days"])).isoformat()
            placeholders = ",".join("?" for _ in DELEGATE_JOB_TERMINAL_STATES)
            report["archive_eligible_total"] = connection.execute(
                f"SELECT COUNT(*) FROM delegate_jobs WHERE state IN ({placeholders}) "
                "AND updated_ts<? AND result_payload IS NOT NULL AND payload_archive_path IS NULL",
                (*DELEGATE_JOB_TERMINAL_STATES, cutoff)).fetchone()[0]
            candidates = connection.execute(
                f"SELECT job_id,result_payload FROM delegate_jobs WHERE state IN ({placeholders}) "
                "AND updated_ts<? AND result_payload IS NOT NULL AND payload_archive_path IS NULL "
                "ORDER BY updated_ts LIMIT ?",
                (*DELEGATE_JOB_TERMINAL_STATES, cutoff, settings["batch_size"])).fetchall()
            report["archive_limit_reached"] = len(candidates) == settings["batch_size"]
            report["archive_remaining"] = max(0, report["archive_eligible_total"] - len(candidates))
            for row in candidates:
                payload_bytes = row["result_payload"].encode("utf-8")
                report["reclaimable_payload_bytes"] += len(payload_bytes)
                archive_path = _delegate_payload_archive_directory() / f"{row['job_id']}.json.gz"
                report["archived"].append({"job_id": row["job_id"], "path": os.fspath(archive_path),
                                           "bytes": len(payload_bytes)})
                if not execute:
                    continue
                archive_path.parent.mkdir(parents=True, exist_ok=True)
                temporary_path = archive_path.with_suffix(archive_path.suffix + ".tmp")
                with temporary_path.open("wb") as raw_stream:
                    with gzip.GzipFile(fileobj=raw_stream, mode="wb") as archive_stream:
                        archive_stream.write(payload_bytes)
                    raw_stream.flush()
                    os.fsync(raw_stream.fileno())
                temporary_path.replace(archive_path)
                with gzip.open(archive_path, "rt", encoding="utf-8") as verification_stream:
                    json.load(verification_stream)
                pointer = json.dumps({"kind": DELEGATE_JOB_ARCHIVE_POINTER_KIND,
                                      "path": os.fspath(archive_path)})
                connection.execute(
                    "UPDATE delegate_jobs SET result_payload=?,payload_archive_path=? "
                    "WHERE job_id=? AND payload_archive_path IS NULL",
                    (pointer, os.fspath(archive_path), row["job_id"]))
    report["more_work_remaining"] = (
        report["reap_limit_reached"] or report["archive_limit_reached"])
    if execute and report["archived"]:
        with _open_delegate_jobs_connection() as connection:
            connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        report["checkpoint"] = True
        database_path = delegate_jobs_database_path()
        required_headroom = database_path.stat().st_size + config.disk_guard_floor_bytes()
        if report["reclaimable_payload_bytes"] < settings["vacuum_min_freed_bytes"]:
            report["vacuum"] = "below-configured-benefit-threshold"
        elif shutil.disk_usage(database_path.parent).free < required_headroom:
            report["vacuum"] = "skipped-insufficient-disk-headroom"
        else:
            with _open_delegate_jobs_connection() as connection:
                connection.execute("VACUUM")
            report["vacuum"] = "completed"
    return report


def record_delegate_codex_thread(thread_id, execution_id):
    """Capture session identity before a transport exception can lose its envelope."""
    if execution_id:
        with _open_delegate_jobs_connection() as connection:
            connection.execute("UPDATE delegate_jobs SET codex_thread_id=? WHERE execution_id=? "
                               "AND codex_session_reaped_at IS NULL", (thread_id, execution_id))


def register_codex_session_lease(thread_id, worker_pid):
    """Hold a thread while its identity-verified pool worker executes or retains it.

    Registration and deletion use the same SQLite writer exclusion. A worker's
    previous idle session stops being held when it starts its next session;
    concurrent active sessions remain protected.
    """
    import psutil
    birth = psutil.Process(worker_pid).create_time()
    with _open_delegate_jobs_connection() as connection:
        connection.execute("DELETE FROM delegate_codex_session_leases "
                           "WHERE worker_pid=? AND worker_birth=? AND active=0 AND thread_id!=?",
                           (worker_pid, birth, thread_id))
        connection.execute("INSERT OR REPLACE INTO delegate_codex_session_leases "
                           "(thread_id,worker_pid,worker_birth,active) VALUES(?,?,?,1)",
                           (thread_id, worker_pid, birth))


def settle_codex_session_lease(thread_id, worker_pid, *, release=False):
    """Retain the worker's idle thread, or release a completed cold invocation."""
    with _open_delegate_jobs_connection() as connection:
        if release:
            connection.execute("DELETE FROM delegate_codex_session_leases WHERE thread_id=? AND worker_pid=?",
                               (thread_id, worker_pid))
        else:
            connection.execute("UPDATE delegate_codex_session_leases SET active=0 "
                               "WHERE thread_id=? AND worker_pid=?", (thread_id, worker_pid))


def reap_terminal_codex_sessions(now=None, *, executable=None, owner_identity_state=None, dead_identity=None):
    """Bounded completion-driven session deletion, never touching Codex databases.

    Hold the journal writer transaction from eligibility recheck through delete
    and receipt so a concurrent resume cannot acquire a lease in between. A
    crash after deletion retries the idempotent CLI primitive on the next tick.
    """
    report = {"reaped": [], "skipped": [], "errors": []}
    try:
        settings = config.codex_session_reap_settings()
        if not settings["enabled"] or not delegate_jobs_database_path().exists():
            return report
        if not executable or owner_identity_state is None or dead_identity is None:
            return report
        current = now or datetime.datetime.now(datetime.timezone.utc)
        success_cutoff = (current - datetime.timedelta(seconds=settings["success_grace_s"])).isoformat()
        failure_cutoff = (current - datetime.timedelta(seconds=settings["failure_grace_s"])).isoformat()
        terminal_placeholders = ",".join("?" for _ in DELEGATE_JOB_TERMINAL_STATES)
        eligibility = (f"state IN ({terminal_placeholders}) AND codex_thread_id IS NOT NULL "
                       "AND codex_session_reaped_at IS NULL AND "
                       "((state=? AND updated_ts<?) OR (state!=? AND updated_ts<?))")
        arguments = (*DELEGATE_JOB_TERMINAL_STATES, DELEGATE_JOB_DONE_STATE, success_cutoff,
                     DELEGATE_JOB_DONE_STATE, failure_cutoff)
        with _open_delegate_jobs_connection() as connection:
            stale_leases = connection.execute(
                "SELECT thread_id,worker_pid,worker_birth FROM delegate_codex_session_leases").fetchall()
            for leased_thread, worker_pid, worker_birth in stale_leases:
                if owner_identity_state(worker_pid, worker_birth) == dead_identity:
                    connection.execute("DELETE FROM delegate_codex_session_leases "
                                       "WHERE thread_id=? AND worker_pid=? AND worker_birth=?",
                                       (leased_thread, worker_pid, worker_birth))
            candidates = connection.execute(
                f"SELECT job_id,codex_thread_id FROM delegate_jobs WHERE {eligibility} "
                "ORDER BY updated_ts,job_id", arguments).fetchall()
        attempts = 0
        handled_threads = set()
        for job_id, thread_id in candidates:
            if attempts >= settings["max_per_pass"]:
                break
            if thread_id in handled_threads:
                continue
            handled_threads.add(thread_id)
            with _open_delegate_jobs_connection() as connection:
                # The first write acquires the cross-process reservation before
                # reads: lease registration cannot slip through the safety check.
                connection.execute("UPDATE delegate_codex_session_leases SET active=active WHERE thread_id=?",
                                   (thread_id,))
                shared = connection.execute(
                    "SELECT job_id,state,updated_ts,codex_session_reaped_at FROM delegate_jobs "
                    "WHERE codex_thread_id=?", (thread_id,)).fetchall()
                eligible = []
                safe = True
                for shared_id, state, terminal_at, reaped_at in shared:
                    cutoff = success_cutoff if state == DELEGATE_JOB_DONE_STATE else failure_cutoff
                    if (state not in DELEGATE_JOB_TERMINAL_STATES
                            or datetime.datetime.fromisoformat(terminal_at) >= datetime.datetime.fromisoformat(cutoff)):
                        safe = False
                    if reaped_at is None:
                        eligible.append(shared_id)
                leases = connection.execute("SELECT worker_pid,worker_birth FROM delegate_codex_session_leases "
                                            "WHERE thread_id=?", (thread_id,)).fetchall()
                if (not safe or not eligible or any(
                        owner_identity_state(pid, birth) != dead_identity
                        for pid, birth in leases)):
                    report["skipped"].append(job_id)
                    continue
                attempts += 1
                failure_reason = None
                try:
                    deleted = subprocess.run([executable, "delete", thread_id, "--force"],
                                             capture_output=True, text=True,
                                             env=config.lane_exec_env(executable),
                                             timeout=settings["delete_timeout_s"])
                    expected_missing = {template.format(thread_id=thread_id)
                                        for template in CODEX_DELETE_MISSING_SESSION_TEMPLATES}
                    missing = any(line.removeprefix("Error: ") in expected_missing
                                  for line in (deleted.stderr or "").splitlines())
                    if deleted.returncode != 0 and not missing:
                        failure_reason = f"codex delete exited {deleted.returncode}: {deleted.stderr}"
                except (OSError, subprocess.SubprocessError) as error:
                    failure_reason = f"{type(error).__name__}: {error}"
                if failure_reason is not None:
                    connection.execute(
                        "UPDATE delegate_jobs SET codex_session_reap_attempts=codex_session_reap_attempts+1,"
                        "codex_session_reap_failure_reason=? WHERE codex_thread_id=? "
                        "AND codex_session_reaped_at IS NULL", (failure_reason, thread_id))
                    failure_attempts = connection.execute(
                        "SELECT MAX(codex_session_reap_attempts) FROM delegate_jobs WHERE codex_thread_id=?",
                        (thread_id,)).fetchone()[0]
                    if failure_attempts >= settings["max_delete_attempts"]:
                        connection.execute(
                            "UPDATE delegate_jobs SET codex_session_reaped_at=?,codex_session_reap_failed_at=? "
                            "WHERE codex_thread_id=? AND codex_session_reaped_at IS NULL",
                            (current.isoformat(), current.isoformat(), thread_id))
                        connection.execute("DELETE FROM delegate_codex_session_leases WHERE thread_id=?", (thread_id,))
                        report["reaped"].extend(eligible)
                    report["errors"].append(f"{job_id}: {failure_reason}")
                    continue
                connection.execute("UPDATE delegate_jobs SET codex_session_reaped_at=? "
                                   "WHERE codex_thread_id=? AND codex_session_reaped_at IS NULL",
                                   (current.isoformat(), thread_id))
                connection.execute("DELETE FROM delegate_codex_session_leases WHERE thread_id=?", (thread_id,))
                report["reaped"].extend(eligible)
    except Exception as error:
        report["errors"].append(f"codex session reap: {error}")
    if report["errors"]:
        config.warn_once(f"[spendguard] codex session reap gaps: {report['errors']}")
    return report


from . import token_operations_accessors as _token_operations_accessors

_token_operations_accessors.set_record_delegate_codex_thread_resolver(record_delegate_codex_thread)
_token_operations_accessors.set_register_codex_session_lease_resolver(register_codex_session_lease)
_token_operations_accessors.set_settle_codex_session_lease_resolver(settle_codex_session_lease)
_token_operations_accessors.set_reap_terminal_codex_sessions_resolver(reap_terminal_codex_sessions)
_token_operations_accessors.set__delegate_job_row_resolver(_delegate_job_row)
_token_operations_accessors.set_recover_delegate_job_execution_resolver(recover_delegate_job_execution)
_token_operations_accessors.set_DELEGATE_JOB_RUNNING_STATE_resolver(lambda: DELEGATE_JOB_RUNNING_STATE)
