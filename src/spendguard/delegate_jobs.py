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
import json
import os
import sqlite3
import subprocess
import sys
import time
import uuid
from pathlib import Path

from . import config
from .process_group_reaper import PROCESS_GROUP_LIVENESS_PROBE_SIGNAL, reap_process_group_bounded

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
DELEGATE_JOB_WAIT_POLL_DEFAULT_S = 0.1
DELEGATE_JOB_CANCEL_GRACE_DEFAULT_S = 5.0
DELEGATE_JOB_CANCEL_GROUP_POLL_DEFAULT_S = 0.05
DELEGATE_JOB_CANCEL_GROUP_WARNING_CONTEXT = "delegate cancel"
DELEGATE_JOB_CANCEL_REASON = "operator-cancelled"
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
    for column in ("codex_thread_id", "codex_session_reaped_at"):
        if column not in columns:
            connection.execute(f"ALTER TABLE delegate_jobs ADD COLUMN {column} TEXT")
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
        terminal = terminalize_durable_execution(row["execution_id"], DELEGATE_JOB_JOURNAL_STATES[state])
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
        return {**payload, **_delegate_job_workspace_details(row), "state": row["state"]} if isinstance(payload, dict) else payload
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
    """Delete terminal job rows older than the configured retention window."""
    cutoff = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(seconds=float(retention_s))
    try:
        with _open_delegate_jobs_connection() as connection:
            placeholders = ",".join("?" for _ in DELEGATE_JOB_TERMINAL_STATES)
            connection.execute(f"DELETE FROM delegate_jobs WHERE state IN ({placeholders}) AND updated_ts<? "
                               "AND (codex_thread_id IS NULL OR codex_session_reaped_at IS NOT NULL)",
                               (*DELEGATE_JOB_TERMINAL_STATES, cutoff.isoformat(timespec="seconds")))
    except sqlite3.OperationalError:
        return


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
                        report["errors"].append(f"{job_id}: codex delete exited {deleted.returncode}: {deleted.stderr}")
                        continue
                except (OSError, subprocess.SubprocessError) as error:
                    report["errors"].append(f"{job_id}: {error}")
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
