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
import pathlib
import sqlite3
import sys
from contextlib import closing

import psutil

from . import config
from .warm_stdio_daemon import WARM_OWNER_BOOT_EPSILON_S

_registered = False                                  # once-per-process guard for register() (cheap hot-path no-op after)
_FLEET_PID_GONE = "gone"
_FLEET_PID_VERIFIED_LIVE = "verified_live"
_FLEET_PID_UNVERIFIABLE = "unverifiable"


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
    # Existing registries predate install-refresh metadata. Keep migration additive so every consumer can continue
    # registering while a newer spendguard gradually rolls through the fleet.
    columns = {row[1] for row in c.execute("PRAGMA table_info(consumers)").fetchall()}
    for name in ("venv_root", "python_executable", "pid_create_time"):
        if name not in columns:
            c.execute("ALTER TABLE consumers ADD COLUMN %s TEXT" % name)
    c.commit()
    return c


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


def _refresh_editable_install(python_executable, repo_root, timeout=180):
    """The pip boundary, kept small so offline tests can replace it without ever invoking a real installer."""
    import subprocess
    result = subprocess.run([str(python_executable), "-m", "pip", "install", "-e", str(repo_root), "--no-cache-dir"],
                            capture_output=True, text=True, timeout=timeout)
    if result.returncode:
        raise RuntimeError((result.stderr or result.stdout or "pip install failed").strip()[:500])


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

    repo_root = pathlib.Path(__file__).resolve().parents[2]
    try:
        current = _dependency_fingerprint(repo_root / "pyproject.toml")
    except Exception as exc:
        return {"refreshed": [], "skipped_current": [],
                "failed": [{"venv": v, "error": "dependency fingerprint: %s" % str(exc)[:300]}
                           for v in distinct], "no_consumers": False}

    for venv, registered_python in distinct.items():
        marker = pathlib.Path(venv) / _INSTALL_FINGERPRINT_MARKER
        try:
            if marker.read_text().strip() == current:
                skipped_current.append(venv)
                continue
        except (OSError, UnicodeError):
            pass
        if not apply:
            refreshed.append(venv)                    # would refresh; result shape stays identical for dry-run
            continue
        try:
            python_executable = pathlib.Path(registered_python) if registered_python else pathlib.Path(venv) / "bin" / "python"
            _refresh_editable_install(python_executable, repo_root)
            marker.write_text(current + "\n")
            refreshed.append(venv)
        except Exception as exc:
            failed.append({"venv": venv, "error": str(exc)[:500]})
    return {"refreshed": refreshed, "skipped_current": skipped_current,
            "failed": failed, "no_consumers": False}


def fleet_summary():
    """One dict for `spendguard fleet` / doctor, including any identity-verification gap. $0."""
    from . import release
    procs = running_consumers()
    g = release.green_pointer() or {}
    return {"green": g.get("short") or g.get("sha"), "n": len(procs),
            "stale": sum(1 for p in procs if p.get("stale")),
            "unverifiable": sum(1 for p in procs if not p.get("identity_verified")), "consumers": procs}
