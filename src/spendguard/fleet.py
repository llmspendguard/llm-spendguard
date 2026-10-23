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
import os
import hashlib
import json
import pathlib
import plistlib
import sqlite3
import subprocess
import sys
from contextlib import closing

import psutil

from . import config
from .warm_stdio_daemon import WARM_OWNER_BOOT_EPSILON_S

_registered = False                                  # once-per-process guard for register() (cheap hot-path no-op after)
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
FLEET_LOG_MAX_BYTES = 5 * 1024 * 1024
FLEET_LOG_KEEP = 1
FLEET_LOG_TRUNCATION_MARKER = "\n[record truncated to fleet log byte cap]\n"
FLEET_LAUNCHD_DIRECTORY_PARTS = ("Library", "LaunchAgents")
FLEET_LAUNCHD_PLIST_SUFFIX = ".plist"
ORPHAN_REAP_LAUNCHD_COMMAND_ARGUMENTS = ("reap-orphans", "--execute", "--json")
QUEUE_MANAGE_LAUNCHD_COMMAND_ARGUMENTS = ("queue-manage", "--execute", "--json")
FLEET_LAUNCHD_OPERATION_TIMEOUT_S = 15
FLEET_TEST_ISOLATION_ENV = "SPENDGUARD_TEST_ISOLATED"
_INTERPRETER_REGISTRATION_TABLE = "registered_interpreters"
_registration_failure = None


def periodic_launchd_job_registry():
    """Package-owned periodic jobs installed by deploy; paths derive from this interpreter and configured home.

    The reaper writes through ``append_bounded_fleet_log_record``; launchd output goes to the null device so its
    append semantics cannot create a second unbounded log.
    """
    spendguard_binary = pathlib.Path(sys.executable).with_name("spendguard")
    orphan_log_path = config.HOME / ORPHAN_REAP_LOG_BASENAME
    queue_log_path = config.HOME / QUEUE_MANAGE_LOG_BASENAME
    return ({"Label": ORPHAN_REAP_LAUNCHD_LABEL,
             "ProgramArguments": [str(spendguard_binary), *ORPHAN_REAP_LAUNCHD_COMMAND_ARGUMENTS,
                                  ORPHAN_REAP_BOUNDED_LOG_FLAG, str(orphan_log_path)],
             "RunAtLoad": True, "StartInterval": ORPHAN_REAP_LAUNCHD_INTERVAL_S,
             "StandardErrorPath": os.devnull, "StandardOutPath": os.devnull},
            {"Label": QUEUE_MANAGE_LAUNCHD_LABEL,
             "ProgramArguments": [str(spendguard_binary), *QUEUE_MANAGE_LAUNCHD_COMMAND_ARGUMENTS,
                                  QUEUE_MANAGE_BOUNDED_LOG_FLAG, str(queue_log_path)],
             "RunAtLoad": True, "StartInterval": QUEUE_MANAGE_LAUNCHD_INTERVAL_S,
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


def _fleet_db():
    """The fleet registry — a DEDICATED tiny db (NOT the spend.db money ledger, NOT lane_queue.db): one row per live
    consumer process, rewritten on register and pruned on read, so it never grows. WAL + a short busy timeout so many
    processes registering their OWN pid never contend. Never raises — a registry hiccup must not break a call."""
    p = str(config.HOME / "fleet.db")
    c = sqlite3.connect(p, timeout=5)
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA busy_timeout=5000")
    c.execute("""CREATE TABLE IF NOT EXISTS consumers(
        pid INTEGER PRIMARY KEY, version TEXT, served_sha TEXT, served_short TEXT, dirty INTEGER,
        cmdline TEXT, launchd_label TEXT, registered_ts TEXT, venv_root TEXT, python_executable TEXT,
        pid_create_time TEXT)""")
    c.execute(f"""CREATE TABLE IF NOT EXISTS {_INTERPRETER_REGISTRATION_TABLE}(
        python_executable TEXT PRIMARY KEY, venv_root TEXT NOT NULL, code_version TEXT,
        metadata_version TEXT, catalog_verified TEXT, first_seen TEXT NOT NULL, last_seen TEXT NOT NULL)""")
    # Existing registries predate install-refresh metadata. Keep migration additive so every consumer can continue
    # registering while a newer spendguard gradually rolls through the fleet.
    columns = {row[1] for row in c.execute("PRAGMA table_info(consumers)").fetchall()}
    for name in ("venv_root", "python_executable", "pid_create_time", "registered_ts"):
        if name not in columns:
            c.execute("ALTER TABLE consumers ADD COLUMN %s TEXT" % name)
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


def registered_interpreters():
    """The persistent authoritative deployment inventory, independent of live process lifetime."""
    with closing(_fleet_db()) as connection:
        rows = connection.execute(
            f"SELECT python_executable,venv_root,code_version,metadata_version,catalog_verified,first_seen,last_seen "
            f"FROM {_INTERPRETER_REGISTRATION_TABLE} ORDER BY python_executable").fetchall()
    keys = ("python_executable", "venv_root", "code_version", "metadata_version", "catalog_verified",
            "first_seen", "last_seen")
    return [dict(zip(keys, row)) for row in rows]


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
        if abs(process.create_time() - recorded) >= WARM_OWNER_BOOT_EPSILON_S:
            return _FLEET_PID_GONE
    except Exception:
        return _FLEET_PID_UNVERIFIABLE
    return _FLEET_PID_VERIFIED_LIVE


def register_consumer(label=None):
    """Record THIS process in the fleet registry — idempotent (once per process), cheap, NEVER raises. Called on the
    first LLM call (adapters.call) and from daemon entrypoints (mcp_server/serve/drain), so every process that could be
    running stale code against a provider is tracked. Stores the EXACT served commit (release.served_sha) + the loaded
    __version__, so staleness is read off the real loaded code, not a start-time guess. `label` is the launchd label
    when the caller knows it; otherwise it is resolved at detection time."""
    global _registered
    if _registered:
        return
    _registered = True                               # set BEFORE the work: a failure must not retry on every call
    try:
        from . import release, _version
        s = release.served_sha() or {}
        now = _fleet_iso()
        try:
            pid_create_time = repr(psutil.Process(os.getpid()).create_time())
        except Exception:
            pid_create_time = None
        with closing(_fleet_db()) as c:
            with c:
                c.execute("INSERT OR REPLACE INTO consumers "
                          "(pid,version,served_sha,served_short,dirty,cmdline,launchd_label,registered_ts,"
                          "venv_root,python_executable,pid_create_time) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                          (os.getpid(), getattr(_version, "__version__", None), s.get("sha"), s.get("short"),
                           1 if s.get("dirty") else 0, _cmdline(), label, now, sys.prefix, sys.executable,
                           pid_create_time))
    except Exception:
        pass


def _fleet_iso():
    import datetime
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


def _launchd_label_for(pid):
    """Best-effort: the launchd label currently managing `pid` (so restart_stale can kickstart it). Parses
    `launchctl list` (pid is column 1, label column 3). None when the pid is not launchd-managed. Never raises."""
    try:
        import subprocess
        out = subprocess.run(["launchctl", "list"], capture_output=True, text=True, timeout=5).stdout
        for ln in out.splitlines():
            parts = ln.split("\t")
            if len(parts) >= 3 and parts[0].strip() == str(pid):
                return parts[2].strip()
    except Exception:
        pass
    return None


def running_consumers(prune=True):
    """The live fleet: one dict per registered process that is still alive, each with `stale` (its served_sha differs
    from the current green pointer — it is running code older/other than what deploy promoted). Dead pids are pruned
    from the registry (default). `stale` is None when it cannot be told (no green pointer, or a consumer with no
    served_sha — a pinned, non-git install — in which case `version` is the only signal). $0, never raises."""
    from . import release
    g = release.green_pointer() or {}
    green_sha = g.get("sha")
    out, gone = [], []
    try:
        with closing(_fleet_db()) as c:
            rows = c.execute("SELECT pid,version,served_sha,served_short,dirty,cmdline,launchd_label,registered_ts,"
                             "pid_create_time "
                             "FROM consumers").fetchall()
            for (pid, ver, sha, short, dirty, cmd, label, reg, recorded_create_time) in rows:
                identity_state = _classify_fleet_pid_identity(pid, recorded_create_time)
                if identity_state == _FLEET_PID_GONE:
                    gone.append((pid, recorded_create_time))
                    continue
                identity_verified = identity_state == _FLEET_PID_VERIFIED_LIVE
                if not identity_verified or not green_sha or not sha:
                    stale = None                      # cannot establish from a sha compare (no pointer / pinned install)
                else:
                    stale = (sha != green_sha)
                out.append({"pid": pid, "version": ver, "served_sha": sha, "served_short": short,
                            "dirty": bool(dirty), "cmdline": cmd, "launchd_label": label, "registered_ts": reg,
                            "pid_create_time": recorded_create_time, "identity_verified": identity_verified,
                            "stale": stale})
            if prune and gone:
                c.executemany("DELETE FROM consumers WHERE pid=? AND pid_create_time IS ?", gone)
                c.commit()
    except Exception:
        pass
    return out


def restart_stale(apply=False):
    """Bring the fleet onto the green commit. For each STALE live consumer, resolve its launchd label; a
    launchd-MANAGED one is kickstarted (`launchctl kickstart -k` → it respawns onto the latest code). An unsupervised
    stale process is NEVER killed (we could not bring it back) — it is returned in `warn` for the operator to restart.
    `apply=False` (default) is a DRY RUN: it reports what it WOULD do without touching anything. Returns
    {restarted:[...], warn:[...], dry_run:bool}. $0, never raises — a restart hiccup is reported, not thrown."""
    import subprocess
    if apply:
        from .codex_broker_owner import ensure_codex_broker_owner_running
        ensure_codex_broker_owner_running()
    restarted, warn = [], []
    for c in running_consumers():
        if not c.get("identity_verified"):
            warn.append({"pid": c["pid"], "cmdline": c["cmdline"],
                         "why": "stale but pid identity could not be verified — not kickstarting"})
            continue
        if not c.get("stale"):                        # only stale ones (None = can't tell → leave it, report in warn)
            if c.get("stale") is None:
                warn.append({"pid": c["pid"], "cmdline": c["cmdline"], "why": "version unknown vs green (pinned install?)"})
            continue
        label = c.get("launchd_label") or _launchd_label_for(c["pid"])
        if not label:
            warn.append({"pid": c["pid"], "cmdline": c["cmdline"], "why": "stale but NOT launchd-managed — restart it manually"})
            continue
        if not apply:
            restarted.append({"pid": c["pid"], "launchd_label": label, "action": "would kickstart"})
            continue
        try:
            uid = os.getuid()
            r = subprocess.run(["launchctl", "kickstart", "-k", "gui/%d/%s" % (uid, label)],
                              capture_output=True, text=True, timeout=15)
            restarted.append({"pid": c["pid"], "launchd_label": label,
                              "action": "kickstarted" if r.returncode == 0 else "kickstart FAILED: %s" % (r.stderr.strip()[:80])})
        except Exception as e:
            restarted.append({"pid": c["pid"], "launchd_label": label, "action": "kickstart error: %s" % str(e)[:80]})
    return {"restarted": restarted, "warn": warn, "dry_run": not apply}


_INSTALL_FINGERPRINT_MARKER = ".spendguard_dep_fingerprint"
_CONSUMER_INSTALL_PROBE_TIMEOUT_S = 15
_CONSUMER_EDITABLE_REFRESH_TIMEOUT_S = 180
_CONSUMER_INSTALL_PROBE = (
    "import importlib.metadata as m,json,pathlib,spendguard;from spendguard import pricing;"
    "d=m.distribution('llm-spendguard');"
    "u=d.read_text('direct_url.json');"
    "a=[];"
    "exec(\"for x in m.distributions():\\n n=(x.metadata.get('Name') or '').lower().replace('_','-')\\n"
    " if n=='llm-spendguard':\\n  q=x.read_text('direct_url.json')\\n"
    "  if q and json.loads(q).get('dir_info',{}).get('editable'): a.append(str(x._path))\");"
    "print(json.dumps({'code_version':spendguard.__version__,'metadata_version':d.version,"
    "'module_path':str(pathlib.Path(spendguard.__file__).resolve()),"
    "'selected_metadata_path':str(d._path),'editable_metadata_paths':a,"
    "'catalog_verified':pricing.PRICING_VERIFIED,"
    "'editable':bool(u and json.loads(u).get('dir_info',{}).get('editable'))}))"
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
    shadowed_editable = (not details["editable"] and module_path.is_relative_to(source_root)
                         and bool(details.get("editable_metadata_paths")))
    details["state"] = (
        "editable_current" if details["editable"] and module_path.is_relative_to(source_root)
        and details["code_version"] == source_version and details["metadata_version"] == source_version
        else "editable_shadowed_by_egg_info" if shadowed_editable
        else "editable_stale" if details["editable"] else "noneditable_pinned"
    )
    return details


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
            cli_paths = [line for line in result.stdout.splitlines() if line]
    if pipx_document is None:
        if isolated_test:
            pipx_document = {}
        else:
            try:
                result = subprocess.run(["pipx", "list", "--json"], capture_output=True, text=True, check=False)
                pipx_document = json.loads(result.stdout) if result.returncode == 0 else {}
            except (OSError, ValueError, json.JSONDecodeError):
                pipx_document = {}
    candidates = []
    for cli_path in cli_paths:
        try:
            first_line = pathlib.Path(cli_path).read_text().splitlines()[0]
        except (OSError, UnicodeError, IndexError):
            continue
        if first_line.startswith("#!"):
            candidates.append({"source": str(pathlib.Path(cli_path).resolve()),
                               "python_executable": first_line[2:].strip().split()[0]})
    for package in (pipx_document.get("venvs") or {}).values():
        metadata = package.get("metadata") or {}
        main_package = metadata.get("main_package") or {}
        if main_package.get("package") != "llm-spendguard":
            continue
        venv_dir = main_package.get("package_or_url") or package.get("venv_dir")
        if venv_dir:
            candidates.append({"source": "pipx", "python_executable": str(pathlib.Path(venv_dir) / "bin" / "python")})
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
            fresh_before = (before["state"] == "editable_current"
                            and before.get("catalog_verified") == pricing.PRICING_VERIFIED)
            if apply and not fresh_before:
                _refresh_editable_install(executable, repo_root)
                updated.append(str(executable))
            after = _consumer_install_state(executable, repo_root, __version__)
            if (after["state"] == "editable_current"
                    and after.get("catalog_verified") == pricing.PRICING_VERIFIED):
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
