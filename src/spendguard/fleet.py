"""The UPDATE-PROPAGATION registry — make "every long-lived consumer is on the latest spendguard" observable AND
enforceable.

THE PROBLEM. A long-lived process loads spendguard's code into memory ONCE at startup and runs THAT code until it
restarts; a later `pip install` / PyPI release / green-pointer promotion does not touch an already-running process.
Measured 2026-10-10: pre-fix 7thsense daemons kept false-firing the codex "LOGGED OUT" toast for DAYS after the fix
shipped, because they had loaded the old `codex_exec` at startup. The spendguard MCP fleet already self-heals (it
checks `release.should_respawn()` and exits so its client respawns it onto the green commit) — but arbitrary consumers
(7thsense, honestreview, the drain, ad-hoc daemons) do not.

THE FIX, three parts, all built on `release.served_sha()` (the EXACT commit this process imported — frozen at import,
never a start-time guess):
  • register()      — each long-lived consumer records (pid, version, served_sha, cmdline) on its first LLM call /
                      daemon start, so we KNOW what code every running process is on.
  • running()       — the live fleet, each row flagged STALE when its served_sha != the green pointer.
  • restart_stale() — `launchctl kickstart -k` the stale launchd-MANAGED consumers (they respawn onto latest); the
                      unsupervised ones are returned as a loud WARN (we never kill a process we cannot bring back).
`spendguard deploy` calls restart_stale() after promoting; `spendguard fleet` shows running(); and
`release.exit_if_stale()` lets any daemon self-exit→respawn. $0, no LLM. Nothing here ever raises onto a call path."""
import atexit
import os
import hashlib
import json
import math
import pathlib
import plistlib
import sqlite3
import subprocess
import sys
import tempfile
import threading
from contextlib import closing

from . import config

_registered_pid = None                               # successful once-per-process register() guard, fork-safe by PID
_consumer_deregister_hook_pid = None
_consumer_registration_lock = threading.Lock()
_FLEET_PID_GONE = "gone"
_FLEET_PID_VERIFIED_LIVE = "verified_live"
_FLEET_PID_UNVERIFIABLE = "unverifiable"
ORPHAN_REAP_LAUNCHD_LABEL = "com.spendguard.codex-orphan-reap"
ORPHAN_REAP_LAUNCHD_INTERVAL_S = 900
ORPHAN_REAP_LOG_BASENAME = "codex-orphan-reap.log"
ORPHAN_REAP_BOUNDED_LOG_FLAG = "--bounded-log"
QUEUE_MANAGE_LAUNCHD_LABEL = "com.spendguard.queue-manage"
QUEUE_MANAGE_LAUNCHD_INTERVAL_S = 300
QUEUE_MANAGE_LOG_BASENAME = "queue-manage.log"
QUEUE_MANAGE_BOUNDED_LOG_FLAG = "--bounded-log"
LANE_LEAK_TRIPWIRE_LAUNCHD_LABEL = "com.spendguard.lane-leak-tripwire"
LANE_LEAK_TRIPWIRE_LAUNCHD_INTERVAL_S = 60 * 60
LANE_LEAK_TRIPWIRE_LOG_BASENAME = "lane-leak-tripwire.log"
LANE_LEAK_TRIPWIRE_BOUNDED_LOG_FLAG = "--bounded-log"
FLEET_SECONDS_PER_HOUR = 60 * 60
FLEET_LOG_MAX_BYTES = 5 * 1024 * 1024
FLEET_LOG_KEEP = 1
FLEET_LOG_TRUNCATION_MARKER = "\n[record truncated to fleet log byte cap]\n"
FLEET_LAUNCHD_DIRECTORY_PARTS = ("Library", "LaunchAgents")
FLEET_LAUNCHD_PLIST_SUFFIX = ".plist"
ORPHAN_REAP_LAUNCHD_COMMAND_ARGUMENTS = ("reap-orphans", "--execute", "--json")
QUEUE_MANAGE_LAUNCHD_COMMAND_ARGUMENTS = ("queue-manage", "--execute", "--json")
LANE_LEAK_TRIPWIRE_LAUNCHD_COMMAND_ARGUMENTS = (
    "lane-leak-check", "--hours", str(LANE_LEAK_TRIPWIRE_LAUNCHD_INTERVAL_S / FLEET_SECONDS_PER_HOUR))
FLEET_LAUNCHD_OPERATION_TIMEOUT_S = 15
FLEET_TEST_ISOLATION_ENV = "SPENDGUARD_TEST_ISOLATED"
FLEET_WHICH_NO_INSTALLATIONS_EXIT_CODE = 1
_INTERPRETER_REGISTRATION_TABLE = "registered_interpreters"
CONSUMER_TABLE_NAME = "consumers"
CONSUMER_ROLE_COLUMN = "role"
CONSUMER_GATE_ENFORCING_COLUMN = "gate_enforcing"
CONSUMER_ROLE_UNSPECIFIED = "unspecified"
CONSUMER_ROLE_DAEMON = "daemon"
CONSUMER_ROLE_EPHEMERAL = "ephemeral"
SUPERVISION_LAUNCHD = "launchd"
CONSUMER_SUPERVISION_MODE_COLUMN = "supervision_mode"
CONSUMER_SUPERVISION_REF_COLUMN = "supervision_ref"
FLEET_RESTARTS_ENABLED_DEFAULT = True
FLEET_RESTARTS_ENABLED_ENV = "SPENDGUARD_FLEET_RESTARTS_ENABLED"
LAUNCHD_SERVICE_NAME_ENV = "XPC_SERVICE_NAME"


def __getattr__(name):
    """Preserve the patchable psutil seam without requiring psutil at module import time."""
    if name != "psutil":
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    import psutil

    globals()[name] = psutil
    return psutil
FLEET_RESTART_FAILURE_KEY = "failed"
FLEET_RESTART_CONFIRMED_KEY = "confirmed"
FLEET_RESTART_REQUESTED_KEY = "restart-requested"
CONSUMER_ROLE_PRECEDENCE = {
    CONSUMER_ROLE_UNSPECIFIED: 0,
    CONSUMER_ROLE_EPHEMERAL: 1,
    CONSUMER_ROLE_DAEMON: 2,
}
FLEET_RESTART_TRANSIENT_KEY = "transient"
FLEET_RESTART_TRANSIENT_EPHEMERAL_REASON = (
    "stale ephemeral one-shot — will exit on its own; not restarted"
)
SQLITE_TABLE_OBJECT_TYPE = "table"
_registration_failure = None


def periodic_launchd_job_registry():
    """Package-owned periodic jobs installed by deploy; paths derive from this interpreter and configured home.

    The reaper writes through ``append_bounded_fleet_log_record``; launchd output goes to the null device so its
    append semantics cannot create a second unbounded log.
    """
    spendguard_binary = pathlib.Path(sys.executable).with_name("spendguard")
    orphan_log_path = config.HOME / ORPHAN_REAP_LOG_BASENAME
    queue_log_path = config.HOME / QUEUE_MANAGE_LOG_BASENAME
    lane_leak_log_path = config.HOME / LANE_LEAK_TRIPWIRE_LOG_BASENAME
    return ({"Label": ORPHAN_REAP_LAUNCHD_LABEL,
             "ProgramArguments": [str(spendguard_binary), *ORPHAN_REAP_LAUNCHD_COMMAND_ARGUMENTS,
                                  ORPHAN_REAP_BOUNDED_LOG_FLAG, str(orphan_log_path)],
             "RunAtLoad": True, "StartInterval": ORPHAN_REAP_LAUNCHD_INTERVAL_S,
             "StandardErrorPath": os.devnull, "StandardOutPath": os.devnull},
            {"Label": QUEUE_MANAGE_LAUNCHD_LABEL,
             "ProgramArguments": [str(spendguard_binary), *QUEUE_MANAGE_LAUNCHD_COMMAND_ARGUMENTS,
                                  QUEUE_MANAGE_BOUNDED_LOG_FLAG, str(queue_log_path)],
             "RunAtLoad": True, "StartInterval": QUEUE_MANAGE_LAUNCHD_INTERVAL_S,
             "StandardErrorPath": os.devnull, "StandardOutPath": os.devnull},
            {"Label": LANE_LEAK_TRIPWIRE_LAUNCHD_LABEL,
             "ProgramArguments": [str(spendguard_binary), *LANE_LEAK_TRIPWIRE_LAUNCHD_COMMAND_ARGUMENTS,
                                  LANE_LEAK_TRIPWIRE_BOUNDED_LOG_FLAG, str(lane_leak_log_path)],
             "RunAtLoad": True, "StartInterval": LANE_LEAK_TRIPWIRE_LAUNCHD_INTERVAL_S,
             "StandardErrorPath": os.devnull, "StandardOutPath": os.devnull})


def append_bounded_fleet_log_record(log_path, record, max_bytes=FLEET_LOG_MAX_BYTES, keep=FLEET_LOG_KEEP):
    """Append one record while bounding the live log and retaining at most ``keep`` rotated generations."""
    path = pathlib.Path(log_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = (record.rstrip("\n") + "\n").encode()
    if len(encoded) > max_bytes:
        marker = FLEET_LOG_TRUNCATION_MARKER.encode()
        encoded = encoded[:max(0, max_bytes - len(marker))] + marker[:max_bytes]
    try:
        current_size = path.stat().st_size
    except FileNotFoundError:
        current_size = 0
    if current_size + len(encoded) > max_bytes:
        for generation in range(keep, 0, -1):
            destination = path.with_name(f"{path.name}.{generation}")
            source = path if generation == 1 else path.with_name(f"{path.name}.{generation - 1}")
            if not source.exists():
                continue
            if destination.exists():
                destination.unlink()
            source.replace(destination)
    with path.open("ab") as stream:
        stream.write(encoded)


def install_periodic_launchd_fleet_jobs():
    """Idempotently install and kickstart every registered periodic fleet job on macOS."""
    if sys.platform != "darwin" or os.environ.get(FLEET_TEST_ISOLATION_ENV):
        return []
    launchd_directory = pathlib.Path.home().joinpath(*FLEET_LAUNCHD_DIRECTORY_PARTS)
    launchd_directory.mkdir(parents=True, exist_ok=True)
    config.HOME.mkdir(parents=True, exist_ok=True)
    domain = "gui/%d" % os.getuid()
    installed = []
    for document in periodic_launchd_job_registry():
        path = launchd_directory / (document["Label"] + FLEET_LAUNCHD_PLIST_SUFFIX)
        encoded = plistlib.dumps(document)
        try:
            current = path.read_bytes()
        except FileNotFoundError:
            current = None
        if current != encoded:
            path.write_bytes(encoded)
        clean_report = None
        if document["Label"] == QUEUE_MANAGE_LAUNCHD_LABEL and current is None:
            from .lane_queue import manage_queue_tick
            clean_report = manage_queue_tick(execute=True, process=False)
            if clean_report.get("error"):
                installed.append({"label": document["Label"], "path": str(path),
                                  "clean_only": clean_report, "install_skipped": True})
                continue
        bootstrap = subprocess.run(["launchctl", "bootstrap", domain, str(path)], capture_output=True, text=True,
                                   timeout=FLEET_LAUNCHD_OPERATION_TIMEOUT_S)
        kickstart = subprocess.run(["launchctl", "kickstart", "%s/%s" % (domain, document["Label"])],
                                   capture_output=True, text=True, timeout=FLEET_LAUNCHD_OPERATION_TIMEOUT_S)
        installed.append({"label": document["Label"], "path": str(path),
                          "bootstrap_rc": bootstrap.returncode, "kickstart_rc": kickstart.returncode,
                          "clean_only": clean_report})
    return installed


def sqlite_snapshot_source_evidence(path):
    """Compare file identity and complete contents without treating reads as writes."""
    if not path.exists():
        return None
    identity = path.stat()
    return ((identity.st_dev, identity.st_ino, identity.st_size,
             identity.st_mtime_ns, identity.st_ctime_ns), path.read_bytes())


def _fleet_db(read_only=False):
    """The fleet registry — a DEDICATED tiny db (NOT the spend.db money ledger, NOT lane_queue.db): one row per live
    consumer process, rewritten on register and pruned on read, so it never grows. WAL + a short busy timeout so many
    processes registering their OWN pid never contend. Never raises — a registry hiccup must not break a call."""
    p = str(config.HOME / "fleet.db")
    if read_only and pathlib.Path(p).exists():
        # Opening even mode=ro on a WAL registry can create persistent -shm/-wal
        # files. Reconstruct its complete committed snapshot in temporary storage
        # instead; preserve WAL and reject any source change during the copy.
        source_paths = (pathlib.Path(p), pathlib.Path(p + "-wal"))
        source_snapshot = {path: sqlite_snapshot_source_evidence(path)
                           for path in source_paths}
        with tempfile.TemporaryDirectory() as registry_snapshot_directory:
            snapshot_path = pathlib.Path(registry_snapshot_directory) / pathlib.Path(p).name
            for source_path, evidence in source_snapshot.items():
                if evidence is not None:
                    (snapshot_path.parent / source_path.name).write_bytes(evidence[1])
            if any(sqlite_snapshot_source_evidence(path) != evidence
                   for path, evidence in source_snapshot.items()):
                raise RuntimeError("consumer registry changed during read-only snapshot")
            with closing(sqlite3.connect(str(snapshot_path))) as snapshot_connection:
                snapshot_connection.execute("PRAGMA query_only=ON")
                snapshot_connection.execute("SELECT count(*) FROM sqlite_master").fetchone()
                snapshot_memory = sqlite3.connect(":memory:")
                try:
                    snapshot_connection.backup(snapshot_memory)
                except Exception:
                    snapshot_memory.close()
                    raise
            return snapshot_memory
    if read_only:
        p = ":memory:"
    else:
        # Registration can precede every other state writer during gate startup.
        # Own this parent at the write seam, never during config import or reads.
        pathlib.Path(p).parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(p, timeout=5)
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA busy_timeout=5000")
    c.execute(f"""CREATE TABLE IF NOT EXISTS consumers(
        pid INTEGER PRIMARY KEY, version TEXT, served_sha TEXT, served_short TEXT, dirty INTEGER,
        cmdline TEXT, launchd_label TEXT, registered_ts TEXT, venv_root TEXT, python_executable TEXT,
        pid_create_time TEXT, {CONSUMER_ROLE_COLUMN} TEXT,
        {CONSUMER_SUPERVISION_MODE_COLUMN} TEXT, {CONSUMER_SUPERVISION_REF_COLUMN} TEXT)""")
    c.execute(f"""CREATE TABLE IF NOT EXISTS {_INTERPRETER_REGISTRATION_TABLE}(
        python_executable TEXT PRIMARY KEY, venv_root TEXT NOT NULL, code_version TEXT,
        metadata_version TEXT, catalog_verified TEXT, first_seen TEXT NOT NULL, last_seen TEXT NOT NULL)""")
    # Existing registries predate install-refresh metadata. Keep migration additive so every consumer can continue
    # registering while a newer spendguard gradually rolls through the fleet.
    columns = {row[1] for row in c.execute("PRAGMA table_info(consumers)").fetchall()}
    for name in ("venv_root", "python_executable", "pid_create_time", "registered_ts"):
        if name not in columns:
            c.execute("ALTER TABLE consumers ADD COLUMN %s TEXT" % name)
    if CONSUMER_ROLE_COLUMN not in columns:
        c.execute("ALTER TABLE consumers ADD COLUMN %s TEXT" % CONSUMER_ROLE_COLUMN)
    if CONSUMER_GATE_ENFORCING_COLUMN not in columns:
        c.execute(f"ALTER TABLE consumers ADD COLUMN {CONSUMER_GATE_ENFORCING_COLUMN} INTEGER")
    for supervision_column in (CONSUMER_SUPERVISION_MODE_COLUMN, CONSUMER_SUPERVISION_REF_COLUMN):
        if supervision_column not in columns:
            c.execute("ALTER TABLE consumers ADD COLUMN %s TEXT" % supervision_column)
    migration_time = _fleet_iso()
    c.execute(
        f"INSERT OR IGNORE INTO {_INTERPRETER_REGISTRATION_TABLE}"
        "(python_executable,venv_root,code_version,metadata_version,catalog_verified,first_seen,last_seen) "
        "SELECT python_executable,venv_root,version,version,NULL,COALESCE(registered_ts,?),COALESCE(registered_ts,?) "
        "FROM consumers WHERE python_executable IS NOT NULL AND python_executable != ''",
        (migration_time, migration_time))
    c.commit()
    return c


def register_enforcing_interpreter():
    """Persist THIS enforcing interpreter as a managed deployment; cheap, network-free, and fail-observable."""
    global _registration_failure
    try:
        import importlib.metadata
        from . import _version, pricing
        python_executable = str(pathlib.Path(sys.executable).resolve())
        now = _fleet_iso()
        try:
            metadata_version = importlib.metadata.version("llm-spendguard")
        except importlib.metadata.PackageNotFoundError:
            metadata_version = None
        with closing(_fleet_db()) as connection:
            with connection:
                connection.execute(
                    f"INSERT INTO {_INTERPRETER_REGISTRATION_TABLE}"
                    "(python_executable,venv_root,code_version,metadata_version,catalog_verified,first_seen,last_seen) "
                    "VALUES(?,?,?,?,?,?,?) ON CONFLICT(python_executable) DO UPDATE SET "
                    "venv_root=excluded.venv_root,code_version=excluded.code_version,"
                    "metadata_version=excluded.metadata_version,catalog_verified=excluded.catalog_verified,"
                    "last_seen=excluded.last_seen",
                    (python_executable, str(pathlib.Path(sys.prefix).resolve()), _version.__version__,
                     metadata_version, pricing.PRICING_VERIFIED, now, now))
        _registration_failure = None
        return True
    except Exception as exc:
        _registration_failure = "%s: %s" % (type(exc).__name__, str(exc))
        return False


def record_enforcing_interpreter_registration_failure(exception=None, captured_stderr=""):
    """Preserve an install-time registration failure for doctor without writing to the install path."""
    global _registration_failure
    failure_details = []
    if exception is not None:
        failure_details.append("%s: %s" % (type(exception).__name__, str(exception)))
    normalized_stderr = captured_stderr.strip()
    if normalized_stderr:
        failure_details.append("captured stderr: %s" % normalized_stderr)
    _registration_failure = "; ".join(failure_details) or "registration failed without diagnostic detail"


def enforcing_interpreter_registration_status():
    """Doctor-facing invariant status; a failed mandatory registration is never silently forgotten."""
    return {"registered": _registration_failure is None, "error": _registration_failure,
            "python_executable": str(pathlib.Path(sys.executable).resolve())}


def registered_interpreters(repo_root=None):
    """Persistent deployment inventory, optionally scoped by recorded venv path containment.

    Registrations store the venv root, not an owning repository. An external/shared venv's
    repository affinity cannot be inferred and is excluded from a repository-scoped result.
    With no scope, preserve the complete inventory independent of live process lifetime.
    """
    with closing(_fleet_db(read_only=True)) as connection:
        rows = connection.execute(
            f"SELECT python_executable,venv_root,code_version,metadata_version,catalog_verified,first_seen,last_seen "
            f"FROM {_INTERPRETER_REGISTRATION_TABLE} ORDER BY python_executable").fetchall()
    keys = ("python_executable", "venv_root", "code_version", "metadata_version", "catalog_verified",
            "first_seen", "last_seen")
    inventory = [dict(zip(keys, row)) for row in rows]
    if repo_root is None:
        return inventory
    resolved_repo_root = pathlib.Path(repo_root).expanduser().resolve()
    return [interpreter for interpreter in inventory
            if interpreter["venv_root"]
            and pathlib.Path(interpreter["venv_root"]).expanduser().resolve().is_relative_to(resolved_repo_root)]


def deregister_interpreter(python_executable):
    """Forget one explicitly retired deployment without changing that interpreter's gate installation."""
    resolved_executable = str(pathlib.Path(python_executable).resolve())
    with closing(_fleet_db()) as connection:
        with connection:
            cursor = connection.execute(
                f"DELETE FROM {_INTERPRETER_REGISTRATION_TABLE} WHERE python_executable=?",
                (resolved_executable,))
            remaining = connection.execute(
                f"SELECT COUNT(*) FROM {_INTERPRETER_REGISTRATION_TABLE}").fetchone()[0]
    return {"removed": cursor.rowcount > 0, "python_executable": resolved_executable, "remaining": remaining}


def _cmdline(pid=None):
    """This (or another) process's command, best-effort via ps — for identifying a consumer in the fleet view. For
    SELF, sys.argv is the cheap source; ps is the cross-process fallback. Never raises."""
    if pid is None or pid == os.getpid():
        try:
            return (" ".join(sys.argv))[:400] or sys.executable
        except Exception:
            return sys.executable
    try:
        import subprocess
        return subprocess.run(["ps", "-o", "command=", "-p", str(pid)], capture_output=True, text=True,
                              timeout=3).stdout.strip()[:400]
    except Exception:
        return ""


def _classify_fleet_pid_identity(pid, recorded_create_time):
    """Classify a registered (pid, create_time) without ever treating uncertainty as death."""
    try:
        psutil = globals()["psutil"]
    except KeyError:
        try:
            import psutil
        except ImportError:
            return _FLEET_PID_UNVERIFIABLE
    from .warm_stdio_daemon import WARM_OWNER_BOOT_EPSILON_S

    try:
        process = psutil.Process(pid)
    except psutil.NoSuchProcess:
        return _FLEET_PID_GONE
    except Exception:
        return _FLEET_PID_UNVERIFIABLE
    if recorded_create_time is None:
        return _FLEET_PID_UNVERIFIABLE
    try:
        recorded = float(recorded_create_time)
    except (TypeError, ValueError):
        return _FLEET_PID_UNVERIFIABLE
    try:
        observed = float(process.create_time())
        if not math.isfinite(recorded) or not math.isfinite(observed):
            return _FLEET_PID_UNVERIFIABLE
        if abs(observed - recorded) >= WARM_OWNER_BOOT_EPSILON_S:
            return _FLEET_PID_GONE
    except Exception:
        return _FLEET_PID_UNVERIFIABLE
    return _FLEET_PID_VERIFIED_LIVE


def _resolve_current_process_supervision(supervision_mode, supervision_ref):
    """Replace an installation naming default with launchd's identity for this process.

    A caller's reference describes its intended installation, not the host's already-installed
    service. XPC_SERVICE_NAME is the identity launchd assigned to this process; without it the
    process cannot honestly claim live launchd supervision.
    """
    if supervision_mode != SUPERVISION_LAUNCHD:
        return supervision_mode, supervision_ref, None
    managing_label = os.environ.get(LAUNCHD_SERVICE_NAME_ENV)
    if not managing_label:
        return None, None, None
    return SUPERVISION_LAUNCHD, managing_label, managing_label


def _update_current_consumer_declarations(role, supervision_mode, supervision_ref):
    """Upgrade this process identity's role and explicit supervision descriptor. Never raises."""
    try:
        try:
            psutil = globals()["psutil"]
        except KeyError:
            import psutil

        incoming_precedence = CONSUMER_ROLE_PRECEDENCE[role] if role is not None else None
        lower_precedence_roles = tuple(
            stored_role for stored_role, stored_precedence in CONSUMER_ROLE_PRECEDENCE.items()
            if incoming_precedence is not None and stored_precedence < incoming_precedence
        )
        if not lower_precedence_roles and supervision_mode is None:
            return
        pid = os.getpid()
        pid_create_time = repr(psutil.Process(pid).create_time())
        with closing(_fleet_db()) as connection:
            with connection:
                table_exists = connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE type=? AND name=?",
                    (SQLITE_TABLE_OBJECT_TYPE, CONSUMER_TABLE_NAME),
                ).fetchone()
                if table_exists and lower_precedence_roles:
                    lower_role_placeholders = ",".join("?" for _role in lower_precedence_roles)
                    connection.execute(
                        f"UPDATE {CONSUMER_TABLE_NAME} SET {CONSUMER_ROLE_COLUMN}=? "
                        f"WHERE pid=? AND pid_create_time=? AND "
                        f"({CONSUMER_ROLE_COLUMN} IS NULL OR "
                        f"{CONSUMER_ROLE_COLUMN} IN ({lower_role_placeholders}))",
                        (role, pid, pid_create_time, *lower_precedence_roles),
                    )
                if table_exists and supervision_mode is not None:
                    connection.execute(
                        f"UPDATE {CONSUMER_TABLE_NAME} SET {CONSUMER_SUPERVISION_MODE_COLUMN}=?,"
                        f"{CONSUMER_SUPERVISION_REF_COLUMN}=?,launchd_label=? "
                        f"WHERE pid=? AND pid_create_time=?",
                        (supervision_mode, supervision_ref, supervision_ref, pid, pid_create_time),
                    )
    except Exception:
        pass


def register_consumer(label=None, role=None, supervision_mode=None, supervision_ref=None):
    """Record THIS process in the fleet registry — idempotent (once per process), cheap, NEVER raises. Called on the
    first LLM call (adapters.call) and from daemon entrypoints (mcp_server/serve/drain), so every process that could be
    running stale code against a provider is tracked. Stores the EXACT served commit (release.served_sha) + the loaded
    __version__, so staleness is read off the real loaded code, not a start-time guess. Supervision is caller-declared;
    no command-line or process inspection is used to infer it. ``label`` remains a legacy storage alias only."""
    global _consumer_deregister_hook_pid, _registered_pid
    _consumer_registration_lock.acquire()
    try:
        current_pid = os.getpid()
        if supervision_mode not in (None, SUPERVISION_LAUNCHD):
            return
        supervision_mode, supervision_ref, discovered_launchd_label = _resolve_current_process_supervision(
            supervision_mode, supervision_ref)
        if _registered_pid == current_pid:
            if role is not None or supervision_mode is not None:
                _update_current_consumer_declarations(role, supervision_mode, supervision_ref)
            return
        resolved_role = CONSUMER_ROLE_UNSPECIFIED if role is None else role
        try:
            from . import release, _version, gate
            # Observe THIS process without require()/install(): those would repair
            # the state being attested. Legacy registrations have no proof.
            try:
                consumer_gate_enforcing = not gate._disabled() and gate._enforcement_armed()
            except Exception:
                consumer_gate_enforcing = False
            s = release.served_sha() or {}
            now = _fleet_iso()
            try:
                try:
                    psutil = globals()["psutil"]
                except KeyError:
                    import psutil

                pid_create_time = repr(psutil.Process(current_pid).create_time())
            except Exception:
                pid_create_time = None
            with closing(_fleet_db()) as c:
                with c:
                    c.execute("INSERT OR REPLACE INTO consumers "
                              "(pid,version,served_sha,served_short,dirty,cmdline,launchd_label,registered_ts,"
                              f"venv_root,python_executable,pid_create_time,{CONSUMER_ROLE_COLUMN},"
                              f"{CONSUMER_SUPERVISION_MODE_COLUMN},{CONSUMER_SUPERVISION_REF_COLUMN},"
                              f"{CONSUMER_GATE_ENFORCING_COLUMN}) "
                              "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                              (current_pid, getattr(_version, "__version__", None), s.get("sha"), s.get("short"),
                               1 if s.get("dirty") else 0, _cmdline(), discovered_launchd_label or label, now,
                               sys.prefix, sys.executable,
                               pid_create_time, resolved_role, supervision_mode, supervision_ref,
                               int(consumer_gate_enforcing)))
            if _consumer_deregister_hook_pid != current_pid:
                atexit.register(_deregister_consumer_at_exit, current_pid, pid_create_time)
                _consumer_deregister_hook_pid = current_pid
            _registered_pid = current_pid
        except Exception:
            pass
    finally:
        _consumer_registration_lock.release()


def deregister_consumer(pid, pid_create_time):
    """Remove exactly one consumer identity; shutdown-time registry failures never escape."""
    try:
        with closing(_fleet_db()) as connection:
            with connection:
                table_exists = connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE type=? AND name=?",
                    (SQLITE_TABLE_OBJECT_TYPE, CONSUMER_TABLE_NAME),
                ).fetchone()
                if table_exists:
                    connection.execute(
                        "DELETE FROM consumers WHERE pid=? AND pid_create_time IS ?",
                        (pid, pid_create_time),
                    )
    except Exception:
        pass


def _deregister_consumer_at_exit(registered_pid, pid_create_time):
    """Deregister only in the process that installed this hook, never in an inherited fork."""
    if os.getpid() == registered_pid:
        deregister_consumer(registered_pid, pid_create_time)


def _fleet_iso():
    import datetime
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


def running_consumers(prune=True):
    """The live fleet: one dict per registered process that is still alive, each with `stale` (its served_sha differs
    from the current green pointer — it is running code older/other than what deploy promoted). Dead pids are pruned
    from the registry (default). `stale` is None when it cannot be told (no green pointer, or a consumer with no
    served_sha — a pinned, non-git install — in which case `version` is the only signal). $0, never raises."""
    from . import release
    g = release.green_pointer() or {}
    green_sha = g.get("sha")
    out, gone = [], []
    inventory_surface = "consumer registry"
    try:
        with closing(_fleet_db(read_only=not prune)) as c:
            consumer_columns = {column[1] for column in c.execute("PRAGMA table_info(consumers)")}
            enforcement_expression = (CONSUMER_GATE_ENFORCING_COLUMN
                                      if CONSUMER_GATE_ENFORCING_COLUMN in consumer_columns else "NULL")
            rows = c.execute(
                "SELECT pid,version,served_sha,served_short,dirty,cmdline,launchd_label,registered_ts,"
                f"pid_create_time,{CONSUMER_ROLE_COLUMN},{CONSUMER_SUPERVISION_MODE_COLUMN},"
                f"{CONSUMER_SUPERVISION_REF_COLUMN},python_executable,{enforcement_expression} FROM consumers"
            ).fetchall()
            for inventory_row in rows:
                inventory_surface = f"consumer row:{inventory_row!r}"
                (pid, ver, sha, short, dirty, cmd, label, reg, recorded_create_time, role,
                 supervision_mode, supervision_ref, python_executable, recorded_enforcement) = inventory_row
                inventory_surface = f"consumer pid:{pid}"
                identity_state = _classify_fleet_pid_identity(pid, recorded_create_time)
                if identity_state == _FLEET_PID_GONE:
                    label_loaded = (launchd_consumer_label_loaded(supervision_ref)
                                    if supervision_mode == SUPERVISION_LAUNCHD else False)
                    if label_loaded is False:
                        gone.append((pid, recorded_create_time))
                        if prune:
                            continue
                identity_verified = identity_state == _FLEET_PID_VERIFIED_LIVE
                if not identity_verified or not green_sha or not sha:
                    stale = None                      # cannot establish from a sha compare (no pointer / pinned install)
                else:
                    stale = (sha != green_sha)
                declared_supervised = supervision_mode == SUPERVISION_LAUNCHD and bool(supervision_ref)
                classification = "stale" if stale and declared_supervised else (
                    "supervised" if declared_supervised else "unsupervised")
                out.append({"pid": pid, "version": ver, "served_sha": sha, "served_short": short,
                            "dirty": bool(dirty), "cmdline": cmd, "launchd_label": label, "registered_ts": reg,
                            "pid_create_time": recorded_create_time, "identity_verified": identity_verified,
                            "liveness": identity_state,
                            "role": role or CONSUMER_ROLE_DAEMON, "stale": stale,
                            "supervision_mode": supervision_mode, "supervision_ref": supervision_ref,
                            "supervised": declared_supervised, "classification": classification,
                            "python_executable": python_executable,
                            "gate_enforcing": (None if recorded_enforcement is None else recorded_enforcement == 1)})
            if prune and gone:
                c.executemany("DELETE FROM consumers WHERE pid=? AND pid_create_time IS ?", gone)
                c.commit()
    except Exception as exc:
        if not prune:
            inventory_error = RuntimeError(f"{inventory_surface} enumeration failed: {exc}")
            inventory_error.surface = inventory_surface
            raise inventory_error from exc
    return out


def fleet_control_restarts_enabled():
    """Return the env-first configured mutation kill switch."""
    raw = os.getenv(FLEET_RESTARTS_ENABLED_ENV)
    if raw is None:
        raw = config._cfg_get("fleet", "restarts_enabled", FLEET_RESTARTS_ENABLED_DEFAULT)
    return raw if isinstance(raw, bool) else str(raw).strip().lower() not in ("0", "false", "no", "off")


def kickstart_declared_launchd_consumer(label):
    """Kickstart one declared launchd label through the package's shared launchctl helper."""
    from .codex_broker_owner import run_declared_launchd_action
    result = run_declared_launchd_action(("kickstart", "-k"), label)
    return {"ok": result.returncode == 0, "detail": result.stderr.strip() or result.stdout.strip()}


def bootout_declared_launchd_consumer(label):
    """Boot out one declared launchd label through the package's shared launchctl helper."""
    from .codex_broker_owner import run_declared_launchd_action
    result = run_declared_launchd_action(("bootout",), label)
    return {"ok": result.returncode == 0, "detail": result.stderr.strip() or result.stdout.strip()}


def launchd_consumer_label_loaded(label):
    """Return whether launchd currently owns ``label``; uncertainty is represented by ``None``."""
    if not label:
        return False
    try:
        from .codex_broker_owner import run_declared_launchd_action
        result = run_declared_launchd_action(("print",), label)
    except Exception:
        return None
    return result.returncode == 0


def restart_stale(apply=False, consumer_id=None, all_supervised=False):
    """Bring the fleet onto the green commit. For each STALE live consumer, resolve its launchd label; a
    launchd-MANAGED one is kickstarted (`launchctl kickstart -k` → it respawns onto the latest code). An unsupervised
    stale process is NEVER killed (we could not bring it back) — daemons are returned in `warn` for the operator to
    restart, while ephemeral one-shots are returned in `transient` because they will exit on their own. `apply=False`
    (default) is a DRY RUN: it reports what it WOULD do without touching anything. Returns
    {restarted:[...], warn:[...], transient:[...], dry_run:bool}. $0, never raises — a restart hiccup is reported,
    not thrown."""
    restarted, requested, confirmed, failed, warn, transient = [], [], [], [], [], []
    green_sha = None
    try:
        from . import release
        green_sha = (release.green_pointer() or {}).get("sha")
    except Exception:
        pass
    enabled = fleet_control_restarts_enabled()
    for c in running_consumers():
        if consumer_id is not None and str(c["pid"]) != str(consumer_id):
            continue
        if not c.get("identity_verified"):
            warn.append({"pid": c["pid"], "cmdline": c["cmdline"],
                         "why": "stale but pid identity could not be verified — not kickstarting"})
            continue
        if not all_supervised and consumer_id is None and not c.get("stale"):
            if c.get("stale") is None:
                warn.append({"pid": c["pid"], "cmdline": c["cmdline"], "why": "version unknown vs green (pinned install?)"})
            continue
        label = c.get("supervision_ref") if c.get("supervision_mode") == SUPERVISION_LAUNCHD else None
        if not label:
            if c.get("role") == CONSUMER_ROLE_EPHEMERAL:
                transient.append({"pid": c["pid"], "cmdline": c["cmdline"],
                                  "why": FLEET_RESTART_TRANSIENT_EPHEMERAL_REASON})
            else:
                warn.append({"pid": c["pid"], "cmdline": c["cmdline"],
                             "why": "cannot restart — not supervised; restart it manually"})
            continue
        if not green_sha:
            warn.append({"pid": c["pid"], "cmdline": c["cmdline"],
                         "why": "cannot restart — no promoted green pointer"})
            continue
        label_loaded = launchd_consumer_label_loaded(label)
        if label_loaded is not True:
            warn.append({"pid": c["pid"], "cmdline": c["cmdline"],
                         "why": ("cannot restart — recorded launchd label is not currently loaded"
                                 if label_loaded is False else
                                 "cannot restart — launchd label state could not be verified")})
            continue
        if not apply:
            restarted.append({"pid": c["pid"], "launchd_label": label, "action": "would kickstart"})
            continue
        if not enabled:
            warn.append({"pid": c["pid"], "cmdline": c["cmdline"],
                         "why": "control-plane restarts disabled by kill switch"})
            continue
        try:
            action = kickstart_declared_launchd_consumer(label)
        except Exception as exc:
            action = {"ok": False, "detail": "%s: %s" % (type(exc).__name__, exc)}
        restarted.append({"pid": c["pid"], "launchd_label": label,
                          "action": "kickstarted" if action.get("ok") else "kickstart FAILED",
                          "detail": action.get("detail")})
        if not action.get("ok"):
            failed.append({"pid": c["pid"], "launchd_label": label,
                           "why": "kickstart failed", "detail": action.get("detail")})
        else:
            requested.append({"pid": c["pid"], "launchd_label": label, "target_sha": green_sha})
    return {"restarted": restarted, FLEET_RESTART_CONFIRMED_KEY: confirmed,
            FLEET_RESTART_REQUESTED_KEY: requested,
            FLEET_RESTART_FAILURE_KEY: failed, "warn": warn,
            FLEET_RESTART_TRANSIENT_KEY: transient, "dry_run": not apply, "disabled": apply and not enabled}


def stop_supervised_consumer(consumer_id, apply=False):
    """Plan or boot out one identity-verified, explicitly launchd-supervised consumer."""
    consumer = next((row for row in running_consumers(prune=False) if str(row["pid"]) == str(consumer_id)), None)
    if consumer is None or not consumer.get("identity_verified"):
        return {"ok": False, "consumer": consumer_id, "why": "consumer identity is absent or not verified"}
    if not consumer.get("supervised"):
        return {"ok": False, "consumer": consumer_id, "why": "cannot stop — not supervised"}
    if not apply:
        return {"ok": True, "consumer": consumer_id, "action": "would bootout",
                "launchd_label": consumer["supervision_ref"], "dry_run": True}
    try:
        result = bootout_declared_launchd_consumer(consumer["supervision_ref"])
    except Exception as exc:
        result = {"ok": False, "detail": "%s: %s" % (type(exc).__name__, exc)}
    return {"ok": bool(result.get("ok")), "consumer": consumer_id,
            "action": "booted out" if result.get("ok") else "bootout FAILED",
            "launchd_label": consumer["supervision_ref"], "detail": result.get("detail"), "dry_run": False}


_INSTALL_FINGERPRINT_MARKER = ".spendguard_dep_fingerprint"
_CONSUMER_INSTALL_PROBE_TIMEOUT_S = 15
_CONSUMER_EDITABLE_REFRESH_TIMEOUT_S = 180
_CONSUMER_INSTALL_PROBE = (
    "import importlib.metadata as m,json,pathlib,spendguard;from spendguard import pricing;"
    "d=None;"
    "exec(\"try:\\n d=m.distribution('llm-spendguard')\\n"
    "except (m.PackageNotFoundError,StopIteration):\\n pass\");"
    "u=d.read_text('direct_url.json') if d else None;"
    "a=[];"
    "exec(\"if d:\\n for x in m.distributions():\\n  n=(x.metadata.get('Name') or '').lower().replace('_','-')\\n"
    "  if n=='llm-spendguard':\\n   q=x.read_text('direct_url.json')\\n"
    "   if q and json.loads(q).get('dir_info',{}).get('editable'): a.append(str(x._path))\");"
    "print(json.dumps({'code_version':spendguard.__version__,'metadata_version':d.version if d else None,"
    "'module_path':str(pathlib.Path(spendguard.__file__).resolve()),"
    "'selected_metadata_path':str(d._path) if d else None,'editable_metadata_paths':a,"
    "'catalog_verified':pricing.PRICING_VERIFIED,"
    "'editable':bool(u and json.loads(u).get('dir_info',{}).get('editable')),"
    "'distribution_present':d is not None}))"
)


def _dependency_fingerprint(pyproject_path=None):
    """Stable hash of the declared dependency set (base + named optional groups). Pure TOML parsing: ordering and
    formatting do not affect it, while adding/removing/moving a requirement does."""
    try:
        import tomllib
    except ModuleNotFoundError:
        import tomli as tomllib
    path = pathlib.Path(pyproject_path) if pyproject_path else pathlib.Path(__file__).resolve().parents[2] / "pyproject.toml"
    with path.open("rb") as fh:
        project = tomllib.load(fh).get("project") or {}
    normalized = ["project:" + "".join(str(dep).split()) for dep in project.get("dependencies", [])]
    for group, dependencies in (project.get("optional-dependencies") or {}).items():
        normalized.extend("optional:%s:%s" % (group, "".join(str(dep).split())) for dep in dependencies)
    return hashlib.sha256("\n".join(sorted(normalized)).encode("utf-8")).hexdigest()


def _refresh_editable_install(python_executable, repo_root, timeout=_CONSUMER_EDITABLE_REFRESH_TIMEOUT_S):
    """Compatibility boundary for tests; installation behavior lives in setup.install-hook machinery."""
    from .setup import refresh_consumer_editable_package
    refresh_consumer_editable_package(python_executable, repo_root, timeout)


def _consumer_install_state(python_executable, repo_root, source_version,
                            timeout=_CONSUMER_INSTALL_PROBE_TIMEOUT_S):
    """Resolve one interpreter's package provenance without importing it into this process."""
    result = subprocess.run([str(python_executable), "-c", _CONSUMER_INSTALL_PROBE],
                            capture_output=True, text=True, timeout=timeout)
    if result.returncode:
        raise RuntimeError((result.stderr or result.stdout or "install probe failed").strip())
    details = json.loads(result.stdout)
    module_path = pathlib.Path(details["module_path"])
    source_root = repo_root / "src"
    if not details.get("distribution_present", True):
        details["state"] = (
            "pathinjected_managed" if module_path.is_relative_to(source_root)
            else "pathinjected_foreign"
        )
        return details
    shadowed_editable = (not details["editable"] and module_path.is_relative_to(source_root)
                         and bool(details.get("editable_metadata_paths")))
    details["state"] = (
        "editable_current" if details["editable"] and module_path.is_relative_to(source_root)
        and details["code_version"] == source_version and details["metadata_version"] == source_version
        else "editable_shadowed_by_egg_info" if shadowed_editable
        else "editable_stale" if details["editable"] else "noneditable_pinned"
    )
    return details


def _consumer_install_is_verified_fresh(install_state, source_version, catalog_version):
    """Freshness is package identity, except that known editable-shadowing is always an invalid deployment."""
    if install_state.get("state") == "pathinjected_managed":
        return (
            install_state.get("code_version") == source_version
            and install_state.get("catalog_verified") == catalog_version
        )
    return (
        install_state.get("state") != "editable_shadowed_by_egg_info"
        and install_state.get("code_version") == source_version
        and install_state.get("metadata_version") == source_version
        and install_state.get("catalog_verified") == catalog_version
    )


def consumer_install_drift():
    """Return actionable install skew for registered consumer venvs; read-only and idempotent."""
    from . import __version__
    repo_root = pathlib.Path(__file__).resolve().parents[2]
    drift = []
    try:
        with closing(_fleet_db()) as connection:
            consumers = connection.execute(
                "SELECT DISTINCT venv_root,python_executable FROM consumers WHERE venv_root IS NOT NULL "
                "AND venv_root != '' ORDER BY venv_root").fetchall()
    except Exception:
        return []
    for venv, registered_python in consumers:
        python_executable = pathlib.Path(registered_python) if registered_python else pathlib.Path(venv) / "bin" / "python"
        try:
            details = _consumer_install_state(python_executable, repo_root, __version__)
            if details["state"] != "editable_current":
                if details["state"] == "editable_shadowed_by_egg_info":
                    remediation = "remove stale in-tree metadata: %s" % details.get("selected_metadata_path")
                else:
                    remediation = f"{python_executable} -m pip install -e {repo_root}"
                drift.append({"venv": venv, **details,
                              "remediation": remediation})
        except Exception as exc:
            drift.append({"venv": venv, "error": str(exc),
                          "remediation": f"{python_executable} -m pip install -e {repo_root}"})
    return drift


def refresh_stale_installs(apply=False):
    """Refresh editable install metadata for each distinct registered consumer venv whose dependency marker differs
    from this checkout. Dry-run only classifies. Failures are isolated per venv and returned, never raised."""
    refreshed, skipped_current, failed = [], [], []
    consumers = []
    try:
        with closing(_fleet_db()) as c:
            consumers = c.execute(
                "SELECT venv_root,python_executable FROM consumers WHERE venv_root IS NOT NULL "
                "AND venv_root != '' ORDER BY venv_root"
            ).fetchall()
    except Exception:
        return {"refreshed": [], "skipped_current": [],
                "failed": [{"venv": None, "error": "could not read fleet registry"}], "no_consumers": True}

    distinct = {}
    for venv_root, python_executable in consumers:
        distinct.setdefault(str(pathlib.Path(venv_root)), python_executable)
    if not distinct:
        return {"refreshed": [], "skipped_current": [], "failed": [], "no_consumers": True}

    from . import __version__
    repo_root = pathlib.Path(__file__).resolve().parents[2]
    try:
        current = _dependency_fingerprint(repo_root / "pyproject.toml")
    except Exception as exc:
        return {"refreshed": [], "skipped_current": [],
                "failed": [{"venv": v, "error": "dependency fingerprint: %s" % str(exc)[:300]}
                           for v in distinct], "no_consumers": False}

    for venv, registered_python in distinct.items():
        marker = pathlib.Path(venv) / _INSTALL_FINGERPRINT_MARKER
        python_executable = pathlib.Path(registered_python) if registered_python else pathlib.Path(venv) / "bin" / "python"
        try:
            install_state = _consumer_install_state(python_executable, repo_root, __version__)
            marker_current = marker.read_text().strip() == current
            if install_state["state"] == "editable_current" and marker_current:
                skipped_current.append(venv)
                continue
        except (OSError, UnicodeError, RuntimeError, ValueError, KeyError, json.JSONDecodeError):
            pass
        if not apply:
            refreshed.append(venv)                    # would refresh; result shape stays identical for dry-run
            continue
        try:
            _refresh_editable_install(python_executable, repo_root)
            marker.write_text(current + "\n")
            refreshed.append(venv)
        except Exception as exc:
            failed.append({"venv": venv, "error": str(exc)[:500]})
    return {"refreshed": refreshed, "skipped_current": skipped_current,
            "failed": failed, "no_consumers": False}


def reconcile_registered_host_installations(discovered=None):
    """Reconcile mechanically discovered spendguard installations against the authoritative registry."""
    installations = discover_host_spendguard_installations() if discovered is None else discovered
    registered = {str(pathlib.Path(row["python_executable"]).resolve()) for row in registered_interpreters()}
    escapes = []
    for installation in installations:
        executable = str(pathlib.Path(installation["python_executable"]).resolve())
        if executable in registered:
            continue
        escapes.append({**installation, "python_executable": executable,
                        "remediation": f"spendguard install-hook --user --python {executable}"})
    return {"complete": not escapes, "discovered": installations, "escapes": escapes}


def discover_host_spendguard_installations(cli_paths=None, pipx_document=None):
    """Discover PATH and pipx installations without guessing logical identity; resolved interpreter path is identity."""
    isolated_test = bool(os.environ.get(FLEET_TEST_ISOLATION_ENV))
    if cli_paths is None:
        if isolated_test:
            cli_paths = []
        else:
            result = subprocess.run(["which", "-a", "spendguard"], capture_output=True, text=True, check=False)
            if result.returncode and (result.returncode != FLEET_WHICH_NO_INSTALLATIONS_EXIT_CODE
                                      or result.stderr or result.stdout):
                raise RuntimeError(f"PATH installation inventory failed: {result.stderr or result.stdout}")
            cli_paths = [line for line in result.stdout.splitlines() if line]
    if pipx_document is None:
        if isolated_test:
            pipx_document = {}
        else:
            try:
                result = subprocess.run(["pipx", "list", "--json"], capture_output=True, text=True, check=False)
                if result.returncode:
                    raise RuntimeError(f"pipx inventory failed: {result.stderr or result.stdout}")
                pipx_document = json.loads(result.stdout)
            except (OSError, ValueError, json.JSONDecodeError) as exc:
                raise RuntimeError(f"pipx inventory unavailable: {exc}") from exc
    candidates = []
    for cli_path in cli_paths:
        try:
            first_line = pathlib.Path(cli_path).read_text().splitlines()[0]
        except (OSError, UnicodeError, IndexError) as exc:
            raise RuntimeError(f"installation {cli_path} could not be inventoried: {exc}") from exc
        if first_line.startswith("#!"):
            candidates.append({"source": str(pathlib.Path(cli_path).resolve()),
                               "python_executable": first_line[2:].strip().split()[0]})
        else:
            raise RuntimeError(f"installation {cli_path} has no interpreter shebang; identity unproven")
    for package in (pipx_document.get("venvs") or {}).values():
        metadata = package.get("metadata") or {}
        main_package = metadata.get("main_package") or {}
        if main_package.get("package") != "llm-spendguard":
            continue
        venv_dir = main_package.get("package_or_url") or package.get("venv_dir")
        if venv_dir:
            candidates.append({"source": "pipx", "python_executable": str(pathlib.Path(venv_dir) / "bin" / "python")})
        else:
            raise RuntimeError("pipx spendguard installation has no interpreter location; identity unproven")
    unique = {}
    for candidate in candidates:
        executable = str(pathlib.Path(candidate["python_executable"]).resolve())
        unique.setdefault(executable, {**candidate, "python_executable": executable})
    return list(unique.values())


def verify_and_refresh_registered_interpreters(apply=False):
    """Touch every registered deployment and prove version + catalog freshness after isolated refresh attempts."""
    from . import __version__, pricing
    repo_root = pathlib.Path(__file__).resolve().parents[2]
    try:
        rows = registered_interpreters()
    except Exception as exc:
        registry_failure = {"python_executable": None,
                            "error": "could not read authoritative registry: %s" % str(exc)[:500]}
        return {"N_total": 0, "N_updated": 0, "N_verified_fresh": 0, "updated": [],
                "verified_fresh": [], "failed": [registry_failure], "complete": False}
    updated, verified_fresh, failed = [], [], []
    for row in rows:
        executable = pathlib.Path(row["python_executable"])
        try:
            before = _consumer_install_state(executable, repo_root, __version__)
            fresh_before = _consumer_install_is_verified_fresh(
                before, __version__, pricing.PRICING_VERIFIED)
            if apply and not fresh_before and before.get("state") != "pathinjected_managed":
                _refresh_editable_install(executable, repo_root)
                updated.append(str(executable))
            after = _consumer_install_state(executable, repo_root, __version__)
            if _consumer_install_is_verified_fresh(after, __version__, pricing.PRICING_VERIFIED):
                verified_fresh.append(str(executable))
            else:
                failed.append({"python_executable": str(executable), "state": after.get("state"),
                               "catalog_verified": after.get("catalog_verified")})
        except Exception as exc:
            failed.append({"python_executable": str(executable), "error": str(exc)[:500]})
    return {"N_total": len(rows), "N_updated": len(updated), "N_verified_fresh": len(verified_fresh),
            "updated": updated, "verified_fresh": verified_fresh, "failed": failed,
            "complete": len(verified_fresh) == len(rows) and not failed}


def fleet_summary():
    """One dict for `spendguard fleet` / doctor, including any identity-verification gap. $0."""
    from . import release
    procs = running_consumers()
    g = release.green_pointer() or {}
    return {"green": g.get("short") or g.get("sha"), "n": len(procs),
            "stale": sum(1 for p in procs if p.get("stale")),
            "unverifiable": sum(1 for p in procs if not p.get("identity_verified")), "consumers": procs}
