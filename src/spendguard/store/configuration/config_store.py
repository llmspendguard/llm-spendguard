"""Base configuration-store paths, knobs, and persistence helpers."""
from __future__ import annotations

import contextlib
import datetime
import math
import os
import pathlib as _pathlib
import threading
from pathlib import Path

from spendguard.ledger_fork_locks import new_fork_reset_ledger_state


DEFAULT_SPENDGUARD_HOME = Path.home() / ".spendguard"
HOME = Path(os.getenv("SPENDGUARD_HOME") or DEFAULT_SPENDGUARD_HOME)
# Resolve paths without creating state during import. Writers create their own
# parent directories; verify-only CLI startup must also work with no state root.


class SingleInstanceLockUnavailableError(RuntimeError):
    """A singleton lock could not establish cross-process exclusion."""



FLAG = HOME / "disabled"                      # persistent kill switch (touch to disable)
LOG = HOME / "gate_log.jsonl"                 # batch-gate audit trail
ANTHROPIC_CACHE = HOME / "anthropic_usage_cache.json"
RT_LOG = HOME / "realtime_log.jsonl"          # real-time spend log (per-day-per-model rollup)


CONFIG_JSON = HOME / "config.json"             # operational (non-secret) config: caps, budget, emit


DELEGATE_JOB_ORPHAN_RUNNING_GRACE_DEFAULT_S = 86400
CODEX_POOL_SIZE_DEFAULT = 20   # warm codex app-servers kept ready per host, fleet-wide default (was 12). The
#   warm lane is the fast path (~5s warm vs >75s cold); a larger floor absorbs concurrent delegate/review fan-out
#   without cold-start churn. An explicit advisor.codex_daemon_pool_size / SPENDGUARD_CODEX_DAEMON_POOL_SIZE still
#   wins per host. Orphaned app-servers from dead hosts are reclaimed by the warm-server reaper, not left to leak.
CODEX_DAEMON_MAX_WAITERS_DEFAULT = 32
CODEX_DAEMON_REQUEST_READ_TIMEOUT_DEFAULT_S = 5.0
CODEX_DAEMON_RECYCLE_ON_TIMEOUT_DEFAULT = True
CODEX_DAEMON_LEASE_IDLE_S_DEFAULT = 120  # Reclaim a leased worker after this long without progress.
CODEX_DAEMON_LEASE_MAX_S_DEFAULT = 1800  # Reclaim a progressing lease at this absolute runaway backstop.
CODEX_POOL_ISOLATE_DEFAULT = False
CODEX_POOL_HEALTH_INTERVAL_DEFAULT_S = 25.0
CODEX_POOL_RESPAWN_BACKOFF_DEFAULT_S = 1.0
CODEX_POOL_RESPAWN_BACKOFF_MULTIPLIER = 2
CODEX_POOL_RESOURCE_EXHAUSTION_BACKOFF_DEFAULT_S = 5.0
CODEX_POOL_RESOURCE_EXHAUSTION_RESTART_THRESHOLD_DEFAULT = 3
CODEX_POOL_OWNER_MARKER_FILENAME = ".spendguard-owner.json"
CODEX_POOL_HOME_STALE_AGE_DEFAULT_S = 3600.0
CODEX_POOL_TRUE_VALUES = frozenset(("1", "true", "yes", "on"))
EXPLICIT_TRUE_VALUES = (True, "true", "1", 1)
WORKSPACE_ROOTS_DEFAULT = ()
WORKSPACE_EXTRA_VENV_DIRS_DEFAULT = ("~/.venvs",)
WORKSPACE_SCAN_DEPTH_DEFAULT = 2
CONSUMER_AUDIT_EXEMPT_DEFAULT = ()


def is_explicit_true(value):
    """The calls-ledger config rule: only canonical explicit values enable a switch."""
    return value in EXPLICIT_TRUE_VALUES


def month_start_utc():
    """First day of the CURRENT month in UTC, as 'YYYY-MM-DD' — the default `since` for every money window.

    Every ledger day-key is written in UTC (budget.record_charge, gate, ledger, receipt, report all use
    datetime.now(timezone.utc)), but the default windows used to be built from `date.today()` — LOCAL time. West
    of UTC that makes the month boundary wrong for 7-8 hours around the 1st: `trust`, `close` and the leak check
    computed a residual that CHANGED depending on what time of day you ran them, and then silently self-corrected
    — the hardest possible bug to chase in an accounting tool. One helper, used everywhere, kills the class."""
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-01")


def today_utc():
    """Today in UTC, as 'YYYY-MM-DD' — the same reason as month_start_utc()."""
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")


def read_json_cache_entry(path, key):
    """Return a shallow copy of a dict cache entry, or None on any cache read failure."""
    try:
        import json as _json
        cached = _json.loads(_pathlib.Path(path).read_text())
        value = cached.get(key)
        return dict(value) if isinstance(value, dict) else None
    except Exception:
        return None


def update_json(path, mutate, reason="", keep_backups=None, required=False, quarantine_unparseable=False):
    """The ONE way this package rewrites a whole JSON file. Every such write goes through here.

    A SWEEP ON 2026-08-10 FOUND 29 WHOLE-FILE JSON WRITES ACROSS 20 MODULES, exactly ONE of them atomic and
    five of them carrying the destructive read:

        try:    data = json.load(open(p))
        except: data = {}                 # <- an unreadable file becomes an EMPTY one
        ...
        p.write_text(json.dumps(data))    # <- and is then replaced by it

    That is not a style problem. It is how ~/.spendguard/config.json went from 9KB of settings to a 26-byte
    probe value with nothing raised and nothing logged, and the same shape sits in the caps registry, the
    receipt cache and the lane state. Each site was individually plausible; the pattern was the defect.

    WHAT THIS GUARANTEES
      * an existing file that will not PARSE is never silently replaced — `required=True` raises,
        otherwise the caller is told and the file is left alone
      * the write is ATOMIC (temp file in the same directory + os.replace), so a crash or a full disk
        leaves the previous file whole rather than truncated
      * EMACS-STYLE BACKUP ON EVERY WRITE, not on request. `path~` always holds the immediately-previous
        version, and `keep_backups` timestamped copies hold deeper history (default from config
        `safety.keep_backups`, 3). This used to default to ZERO — the capability existed and almost every
        caller left it off, which is why "every destructive mutation backs up first" came back ABSENT from
        a whole-repo invariant check even after this function was written. A safety default that has to be
        remembered is one that will be forgotten; that is the same failure that lost config.json in the
        first place, one level up. Pass keep_backups=0 to keep only `path~`.

    `mutate(data)` edits in place or returns a new object. Returns the written object, or None if the
    write was declined."""
    import json as _json
    import os as _os
    import datetime as _dt
    path = _pathlib.Path(path)

    cur = {}
    if path.exists():
        try:
            cur = _json.loads(path.read_text() or "{}")
        except Exception as e:
            msg = (f"{path} exists but does not parse ({type(e).__name__}: {str(e)[:60]}) — REFUSING to "
                   f"rewrite it, because doing so would replace its whole contents with this one change.")
            if quarantine_unparseable:
                # SETTINGS AND CACHES NEED OPPOSITE ANSWERS HERE, and only the caller knows which it holds.
                # Refusing forever is right for config.json — those settings exist nowhere else, so a
                # corrupt file must be repaired by a human, not overwritten. It is wrong for an adapter's
                # state file, which is REBUILDABLE from the transcripts: refusing there wedges the adapter
                # permanently, every later save failing on damage that will never repair itself. So the
                # damaged file is MOVED ASIDE (kept, for forensics) and a fresh one written. Nothing is
                # destroyed either way; the difference is whether recovery is automatic.
                import sys as _sys
                stamp = _dt.datetime.now(_dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
                bad = path.with_suffix(path.suffix + f".corrupt.{stamp}")
                try:
                    _os.replace(path, bad)
                    _sys.stderr.write(f"[spendguard] WARN {path} did not parse — moved to {bad.name} and "
                                      f"rebuilding. Nothing was deleted.\n")
                    # BOUND the quarantine: these are REBUILDABLE caches kept for forensics, not forever. Unpruned,
                    # 142 `resource_state_state.json.corrupt.*` copies had accumulated (2026-10-09) — a recurring
                    # corruption leaking disk. Keep the most recent N per base file (stamp sorts lexically); drop
                    # the rest. Scoped to THIS base name so one cache's churn never prunes another's.
                    _kc = keep_corrupt_default()
                    _sibs = sorted(path.parent.glob(path.name + ".corrupt.*"))
                    for _old in (_sibs[:-_kc] if _kc else _sibs):
                        try:
                            _old.unlink()
                        except OSError:
                            pass
                except Exception:
                    _sys.stderr.write(f"[spendguard] WARN {msg}\n")
                    return None
                cur = {}
            else:
                if required:
                    raise ValueError(msg)
                import sys as _sys
                _sys.stderr.write(f"[spendguard] WARN {msg}\n")
                return None

    out = mutate(cur)
    if out is None:
        out = cur

    path.parent.mkdir(parents=True, exist_ok=True)
    # `path~` — the previous version, ALWAYS, before anything replaces it. Cheap, bounded (one copy), and
    # it is the file you actually want at 2am: not a timestamped archive to hunt through, just "what it was
    # a moment ago", exactly where Emacs puts it.
    if path.exists():
        try:
            _pathlib.Path(str(path) + "~").write_bytes(path.read_bytes())
        except Exception:
            pass                                  # a failed backup must not block a legitimate write
    if keep_backups is None:
        keep_backups = keep_backups_default()
    if keep_backups and path.exists():
        stamp = _dt.datetime.now(_dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        tag = "".join(c if c.isalnum() or c in "-_" else "-" for c in (reason or "save"))[:32]
        try:
            (path.parent / f"{path.stem}.{stamp}.{tag}{path.suffix}").write_text(path.read_text())
            for old in sorted(path.parent.glob(f"{path.stem}.2*{path.suffix}"))[:-keep_backups]:
                old.unlink(missing_ok=True)
        except Exception:
            pass                                  # a failed backup must not block a legitimate write
    # UNIQUE staging file per writer. A FIXED `<name>.tmp` was SHARED by every process writing this path, and these
    # state files are written CONCURRENTLY across processes (resource_state._save fires on the hot dispatch path from
    # the scheduled reconcile, lane-drain, MCP servers and hook subprocesses; the per-process threading.Lock does not
    # reach across them). Two writers open the same `.tmp`, their write_text calls interleave (each open truncates),
    # and os.replace then promotes the MIXED bytes → a torn JSON the next reader quarantines. That is what produced
    # 140 resource_state_state.json.corrupt.* copies (2026-10-09). A per-writer staging name (pid + random) makes
    # every os.replace promote a COMPLETE file — last writer wins, every intermediate state parses.
    tmp = path.parent / f"{path.name}.{_os.getpid()}.{uid(8)}.tmp"
    try:
        tmp.write_text(_json.dumps(out, indent=2))
        _os.replace(tmp, path)                    # atomic: readers never see a half-written file
    finally:
        try:
            tmp.unlink()                          # only still here if write/replace failed — never leak a staging file
        except OSError:
            pass
    return out


def save_config(mutate, reason=""):
    """The ONLY safe way to change config.json. Read-modify-write, but with the three things the three
    hand-rolled copies of this were missing.

    WHAT WENT WRONG WITHOUT IT (2026-08-10). setup.py, chat.py x2 and pricing.set_price each opened the
    file with `except Exception: cfg = {}` and then rewrote it whole — so ONE unreadable byte replaced
    every setting with whatever key that caller happened to be setting. Separately, a test wrote the path
    directly and reduced ~9KB of settings to 26 bytes. There was no backup, and the loss was silent: the
    tool kept running on schema defaults, `calls.enabled` quietly became False, and the next review wave's
    independent ledger cross-check read $0.00 as a consequence.

    THE THREE GUARANTEES
      1. AN UNREADABLE FILE IS NEVER OVERWRITTEN. If it exists and will not parse, this raises. Refusing to
         save one setting is recoverable; replacing every setting is not.
      2. THE WRITE IS ATOMIC. Written to a temp file in the same directory and os.replace()d, so a crash or
         a full disk leaves the previous file intact rather than a truncated one.
      3. THE PREVIOUS VERSION IS KEPT. A rolling set of timestamped backups, because "I set that last week"
         has to be answerable.

    `mutate` receives the parsed dict and modifies it in place (or returns a new one)."""
    import json as _json
    import os as _os
    import datetime as _dt

    cur = {}
    if CONFIG_JSON.exists():
        try:
            cur = _json.loads(CONFIG_JSON.read_text())
        except Exception as e:
            raise ValueError(
                f"{CONFIG_JSON} exists but does not parse ({type(e).__name__}: {str(e)[:80]}). REFUSING to "
                f"write, because saving would replace every setting in it with just this change. Fix or "
                f"move that file first — a copy of the last good version is in {CONFIG_JSON.parent}.")
        if not isinstance(cur, dict):
            raise ValueError(f"{CONFIG_JSON} does not contain a JSON object — refusing to overwrite it.")

    out = mutate(cur)
    if out is None:
        out = cur
    if not isinstance(out, dict):
        raise ValueError("save_config: the mutate function must leave a dict")

    HOME.mkdir(parents=True, exist_ok=True)
    if CONFIG_JSON.exists():                       # keep the version we are about to replace
        stamp = _dt.datetime.now(_dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        tag = "".join(c if c.isalnum() or c in "-_" else "-" for c in (reason or "save"))[:32]
        try:
            (CONFIG_JSON.parent / f"config.{stamp}.{tag}.json").write_text(CONFIG_JSON.read_text())
            olds = sorted(CONFIG_JSON.parent.glob("config.2*.json"))[:-10]
            for f in olds:
                f.unlink(missing_ok=True)
        except Exception:
            pass                                   # a failed backup must not block a legitimate save
    tmp = CONFIG_JSON.with_suffix(".json.tmp")
    tmp.write_text(_json.dumps(out, indent=2))
    _os.replace(tmp, CONFIG_JSON)                  # atomic: never a half-written config
    cfg_invalidate()
    return out


def cfg_invalidate():
    """Drop the config cache. Call after WRITING config.json in-process.

    _cfg() cached on first read and never re-checked, so a `config set` followed by a read in the SAME
    process returned the value from before the write — the CLI wrote the file, reported success, and then
    acted on the old setting. A cache with no invalidation is a fact frozen at whatever time it was first
    needed."""
    _cfg._cache = None
    _cfg._mtime = None


class ConfigurationReadError(RuntimeError):
    """Configuration could not be read; defaults must not authorize metered fallback."""


def _cfg():
    """~/.spendguard/config.json (cached, and re-read when the file changes on disk)."""
    import json as _json
    # THE MTIME IS PART OF THE CACHE KEY. Another process (or `config set` in this one) can rewrite the
    # file at any time, and a stale read of a settings file is how a knob appears not to work.
    try:
        _m = CONFIG_JSON.stat().st_mtime
    except FileNotFoundError:
        _m = None
    except Exception as error:
        cfg_invalidate()
        raise ConfigurationReadError(
            f"Cannot inspect spendguard config {CONFIG_JSON}: {error}. "
            "Restore readable configuration before routing work; metered fallback is refused."
        ) from error
    if getattr(_cfg, "_mtime", "unset") != _m:
        _cfg._cache = None
    if getattr(_cfg, "_cache", None) is None:
        c = {}
        try:
            if _m is not None:
                c = _json.loads(CONFIG_JSON.read_text())
            if not isinstance(c, dict):
                raise ValueError("configuration must be a JSON object")
        except Exception as error:
            cfg_invalidate()
            raise ConfigurationReadError(
                f"Cannot read spendguard config {CONFIG_JSON}: {error}. "
                "Repair the configuration before routing work; metered fallback is refused."
            ) from error
        _cfg._cache = c
    _cfg._mtime = _m                       # commit only a successful read (or confirmed absence)
    return _cfg._cache


def _cfg_get(section, key, default=None):
    return (_cfg().get(section) or {}).get(key, default)


def consumer_audit_settings(checkout_path=None):
    """Resolve consumer-audit roots without embedding a machine-specific workspace path."""
    checkout = Path(checkout_path or Path(__file__).resolve().parents[2]).resolve()
    configured_roots = _cfg().get("workspace_roots", WORKSPACE_ROOTS_DEFAULT)
    configured_extras = _cfg().get("workspace_extra_venv_dirs", WORKSPACE_EXTRA_VENV_DIRS_DEFAULT)
    configured_exempt = _cfg().get("consumer_audit_exempt", CONSUMER_AUDIT_EXEMPT_DEFAULT)
    scan_depth = int(_cfg().get("workspace_scan_depth", WORKSPACE_SCAN_DEPTH_DEFAULT))
    roots = configured_roots or (str(checkout.parent),)
    return {
        "workspace_roots": tuple(Path(value).expanduser().resolve() for value in roots),
        "workspace_extra_venv_dirs": tuple(Path(value).expanduser().resolve() for value in configured_extras),
        "consumer_audit_exempt": tuple(Path(value).expanduser().resolve() for value in configured_exempt),
        "workspace_scan_depth": scan_depth,
    }


CODEX_SESSION_REAP_DEFAULTS = {
    "enabled": True, "success_grace_s": 300, "failure_grace_s": 600,
    "max_per_pass": 200, "delete_timeout_s": 30, "max_delete_attempts": 3,
}


def codex_session_reap_settings():
    """Resolve and validate completion-driven retention through the config SSOT."""
    settings = {key: _cfg_get("codex_session_reap", key, value)
                for key, value in CODEX_SESSION_REAP_DEFAULTS.items()}
    if not isinstance(settings["enabled"], bool):
        raise ValueError("codex_session_reap.enabled must be boolean")
    for key in ("success_grace_s", "failure_grace_s", "delete_timeout_s"):
        settings[key] = float(settings[key])
        if not math.isfinite(settings[key]) or settings[key] < 0:
            raise ValueError(f"codex_session_reap.{key} must be finite and nonnegative")
    for key in ("max_per_pass", "max_delete_attempts"):
        cap = settings[key]
        if isinstance(cap, bool) or not isinstance(cap, int) or cap <= 0:
            raise ValueError(f"codex_session_reap.{key} must be a positive integer")
    if settings["delete_timeout_s"] <= 0:
        raise ValueError("codex_session_reap.delete_timeout_s must be positive")
    return settings


def codex_daemon_pool_settings():
    """Pool settings, with explicit environment overrides taking precedence over advisor config."""
    size = int(os.getenv("SPENDGUARD_CODEX_DAEMON_POOL_SIZE",
                         _cfg_get("advisor", "codex_daemon_pool_size", CODEX_POOL_SIZE_DEFAULT)))
    if size <= 0:
        raise ValueError("advisor.codex_daemon_pool_size must be positive")
    isolated = os.getenv("SPENDGUARD_CODEX_DAEMON_POOL_ISOLATE_HOMES")
    if isolated is None:
        isolated = _cfg_get("advisor", "codex_daemon_pool_isolate_homes", CODEX_POOL_ISOLATE_DEFAULT)
    if isinstance(isolated, str):
        isolated = isolated.lower() in CODEX_POOL_TRUE_VALUES
    health_interval_s = float(os.getenv(
        "SPENDGUARD_CODEX_DAEMON_POOL_HEALTH_INTERVAL_S",
        _cfg_get("advisor", "codex_daemon_pool_health_interval_s", CODEX_POOL_HEALTH_INTERVAL_DEFAULT_S)))
    if not math.isfinite(health_interval_s) or health_interval_s <= 0:
        raise ValueError("advisor.codex_daemon_pool_health_interval_s must be positive and finite")
    respawn_backoff_s = float(_cfg_get("advisor", "codex_daemon_pool_respawn_backoff_s",
                                     CODEX_POOL_RESPAWN_BACKOFF_DEFAULT_S))
    if not math.isfinite(respawn_backoff_s) or respawn_backoff_s <= 0:
        raise ValueError("advisor.codex_daemon_pool_respawn_backoff_s must be positive and finite")
    resource_backoff_s = float(_cfg_get(
        "advisor", "codex_daemon_pool_resource_exhaustion_backoff_s",
        CODEX_POOL_RESOURCE_EXHAUSTION_BACKOFF_DEFAULT_S))
    if not math.isfinite(resource_backoff_s) or resource_backoff_s <= 0:
        raise ValueError("advisor.codex_daemon_pool_resource_exhaustion_backoff_s must be positive and finite")
    restart_threshold = _cfg_get(
        "advisor", "codex_daemon_pool_resource_exhaustion_restart_threshold",
        CODEX_POOL_RESOURCE_EXHAUSTION_RESTART_THRESHOLD_DEFAULT)
    if isinstance(restart_threshold, bool) or not isinstance(restart_threshold, int) or restart_threshold <= 0:
        raise ValueError("advisor.codex_daemon_pool_resource_exhaustion_restart_threshold must be a positive integer")
    return {"size": size, "isolate_homes": bool(isolated), "health_interval_s": health_interval_s,
            "respawn_backoff_s": respawn_backoff_s, "resource_exhaustion_backoff_s": resource_backoff_s,
            "resource_exhaustion_restart_threshold": restart_threshold}


def codex_daemon_admission_settings():
    """Bound admission and enable deadline recovery; environment overrides advisor settings."""
    max_waiters = int(os.getenv("SPENDGUARD_CODEX_DAEMON_MAX_WAITERS", _cfg_get(
        "advisor", "codex_daemon_max_waiters", CODEX_DAEMON_MAX_WAITERS_DEFAULT)))
    read_timeout = float(os.getenv("SPENDGUARD_CODEX_DAEMON_REQUEST_READ_TIMEOUT_S", _cfg_get(
        "advisor", "codex_daemon_request_read_timeout_s", CODEX_DAEMON_REQUEST_READ_TIMEOUT_DEFAULT_S)))
    if max_waiters < 0:
        raise ValueError("advisor.codex_daemon_max_waiters must be nonnegative")
    if not math.isfinite(read_timeout) or read_timeout <= 0:
        raise ValueError("advisor.codex_daemon_request_read_timeout_s must be positive and finite")
    recycle = os.getenv("SPENDGUARD_CODEX_DAEMON_RECYCLE_ON_TIMEOUT", _cfg_get(
        "advisor", "codex_daemon_recycle_on_timeout", CODEX_DAEMON_RECYCLE_ON_TIMEOUT_DEFAULT))
    if isinstance(recycle, str):
        recycle = recycle.lower() in CODEX_POOL_TRUE_VALUES
    lease_idle_s = float(os.getenv("SPENDGUARD_CODEX_DAEMON_LEASE_IDLE_S", _cfg_get(
        "advisor", "codex_daemon_lease_idle_s", CODEX_DAEMON_LEASE_IDLE_S_DEFAULT)))
    lease_max_s = float(os.getenv("SPENDGUARD_CODEX_DAEMON_LEASE_MAX_S", _cfg_get(
        "advisor", "codex_daemon_lease_max_s", CODEX_DAEMON_LEASE_MAX_S_DEFAULT)))
    if not math.isfinite(lease_idle_s) or lease_idle_s <= 0:
        raise ValueError("advisor.codex_daemon_lease_idle_s must be positive and finite")
    if not math.isfinite(lease_max_s) or lease_max_s <= 0:
        raise ValueError("advisor.codex_daemon_lease_max_s must be positive and finite")
    return {"max_waiters": max_waiters, "request_read_timeout_s": read_timeout,
            "recycle_on_timeout": bool(recycle), "lease_idle_s": lease_idle_s,
            "lease_max_s": lease_max_s}


def codex_daemon_worker_home_stale_age_s():
    """Minimum dead-owner worker-home age before orphan reclamation."""
    value = os.getenv("SPENDGUARD_CODEX_DAEMON_HOME_STALE_AGE_S", _cfg_get(
        "advisor", "codex_daemon_home_stale_age_s", CODEX_POOL_HOME_STALE_AGE_DEFAULT_S))
    age_s = float(value)
    if not math.isfinite(age_s) or age_s < 0:
        raise ValueError("advisor.codex_daemon_home_stale_age_s must be nonnegative and finite")
    return age_s


def _advisor_coerced(name, default, coerce):
    """Read an `advisor.<name>` config knob and coerce it with `coerce` (e.g. float, or type(default)), defaulted —
    the ONE place the advisor-knob read + fallback logic lives, so no caller keeps a divergent copy. Uses `v is None`,
    NOT `v or default`: a value the operator deliberately set to 0 is falsy, so `... or default` would silently
    replace an explicit 0 with the default. None (unset) → the coerced default; a bad value → the default unchanged."""
    v = _cfg_get("advisor", name, None)
    try:
        return coerce(default if v is None else v)
    except (TypeError, ValueError):
        return default


def advisor_float(name, default):
    """An `advisor.<name>` knob as a FLOAT, defaulted (lane_balance and lane_bandit delegate here — no divergent
    copies). Explicit-0 / unset / bad-value rules: see _advisor_coerced."""
    return _advisor_coerced(name, default, float)


def advisor_num(name, default):
    """An `advisor.<name>` knob coerced to the DEFAULT'S type — an int default → int, a float default → float —
    defaulted. For callers whose knob has a declared integer OR float type (the lane_queue parameters), so an int
    knob is not silently widened to float. Same explicit-0 / unset / bad-value rules as advisor_float; see
    _advisor_coerced."""
    return _advisor_coerced(name, default, type(default))


def escalation_ratio():
    """Maximum metered->metered escalation multiple. Env wins over config."""
    raw = os.getenv("SPENDGUARD_ESCALATION_RATIO")
    if raw is None:
        raw = _cfg_get("caps", "escalation_ratio", 3.0)
    try:
        value = float(raw)
        return value if value > 0 else 3.0
    except (TypeError, ValueError):
        return 3.0


def escalation_baseline_epsilon():
    """Baseline dollars at or below which the relative gate defers to the absolute cap."""
    raw = os.getenv("SPENDGUARD_ESCALATION_BASELINE_EPSILON")
    if raw is None:
        raw = _cfg_get("caps", "escalation_baseline_epsilon", 0.000001)
    try:
        value = float(raw)
        return value if value >= 0 else 0.000001
    except (TypeError, ValueError):
        return 0.000001


def shed_lanes():
    """The SET of lanes the operator marked to PROACTIVELY shed from (`advisor.shed_lanes`, a comma list) — the ONE
    place this knob is parsed, so EVERY router honours the SAME shed directive and they can never drift. A shed lane
    is held out of DISCRETIONARY routing (substitutable work goes to the other free lanes) to preserve this plan's
    quota — provider-agnostic, works for any lane. Empty set = shed only when a lane is actually capped/on overage.
    Readers: plan_admission._shed_set (the admission-control shed) AND route_utility.rank_lanes (the bandit/load-
    balance destination ranking) — before this was the SSOT, the bandit ignored the directive and kept routing ONTO a
    shed lane, defeating the shed for every fungible call. Never raises — a shed read must not break routing."""
    try:
        raw = str(_cfg_get("advisor", "shed_lanes", "") or "")
        return {s.strip() for s in raw.split(",") if s.strip()}
    except Exception:
        return set()


_GITROOT_CACHE = {}


def git_root_project(cwd):
    """Repo name for a cwd = the git-root basename, lowercased (so a session's SUBDIR — lmm/scripts/fanout —
    collapses to the repo, `lmm`, instead of fragmenting into `fanout`). Matches how the gate tags actual-$ charges
    (budget._project). Cached per dir; returns None when cwd isn't inside a git repo (caller falls back to basename)."""
    if not cwd:
        return None
    key = str(cwd)
    if key in _GITROOT_CACHE:
        return _GITROOT_CACHE[key]
    out = None
    try:
        import subprocess
        root = subprocess.run(["git", "-C", key, "rev-parse", "--show-toplevel"],
                              capture_output=True, text=True, timeout=2).stdout.strip()
        if root:
            out = os.path.basename(root).strip().lower() or None
    except Exception:
        out = None
    _GITROOT_CACHE[key] = out
    return out


def cap():
    """Per-batch hard cap ($). env GATE_CAP → config.json caps.per_batch → 75."""
    v = os.getenv("GATE_CAP")
    return float(v) if v is not None else float(_cfg_get("caps", "per_batch", 75))


def rt_budget():
    """Cumulative real-time cap ($). env GATE_RT_BUDGET → config.json caps.realtime → 50."""
    v = os.getenv("GATE_RT_BUDGET")
    return float(v) if v is not None else float(_cfg_get("caps", "realtime", 50))


def _policy_cap(cls, window):
    """The org/team's SERVER-pushed cap for (cls, window): {"usd", "mode"} or None. Cached in config.json `policy`
    by `spendguard saas sync` (saas.pull_policy). mode = advisory | enforced."""
    try:
        node = ((_cfg().get("policy") or {}).get("caps") or {}).get(cls) or {}
        v = node.get(window)
        if isinstance(v, dict) and v.get("usd") is not None:
            return {"usd": float(v["usd"]), "mode": v.get("mode", "advisory")}
    except ConfigurationReadError:
        raise
    except Exception:
        pass
    return None


def policy_caps():
    """The full server-pushed policy {caps:{cls:{window:{usd,mode}}}, asof, pulled_at} — for doctor/receipt to
    surface an advisory org suggestion or an enforced ceiling. Empty dict when none pulled."""
    return _cfg().get("policy") or {}


def class_cap(cls, window):
    """Resource-class spend cap ($) — cls in {total, llm, compute}, window in {daily, monthly}. None = off.
    LOCAL order: env GATE_{CLS}_{WINDOW} (e.g. GATE_LLM_DAILY) → nested config caps.{cls}.{window} → (total only)
    legacy flat caps.{window}. Then the SERVER policy (central caps): an ENFORCED org/team cap is a hard ceiling —
    effective = min(local, enforced), applied even with no local cap (local may only TIGHTEN it, never loosen). An
    ADVISORY policy cap is a suggestion only (surfaced by doctor/receipt) — it does NOT change the effective cap
    (partner, not supervisor)."""
    local = None
    env = os.getenv(f"GATE_{cls.upper()}_{window.upper()}")
    if env is not None:
        local = float(env)
    else:
        caps = _cfg().get("caps") or {}
        flat = caps.get(f"{cls}.{window}")                    # how init/config stores it: caps["llm.daily"]
        if flat is not None:
            local = float(flat)
        else:
            node = caps.get(cls)                              # nested form: caps["llm"]["daily"]
            if isinstance(node, dict) and node.get(window) is not None:
                local = float(node[window])
            elif cls == "total" and caps.get(window) is not None:   # legacy flat caps.daily/monthly == total ceiling
                local = float(caps[window])
    pol = _policy_cap(cls, window)
    if pol and pol.get("mode") == "enforced":                 # org-enforced ceiling; local may only tighten it
        return pol["usd"] if local is None else min(local, pol["usd"])
    return local


def daily_cap():    return class_cap("total", "daily")          # back-compat: the total daily ceiling
def monthly_cap():  return class_cap("total", "monthly")


def meta_cap():
    """Separate daily $ cap for spendguard's OWN advisor LLM use (intent spendguard:*). Default $2/day."""
    v = os.getenv("GATE_META_BUDGET")
    return float(v) if v is not None else float(_cfg_get("caps", "meta", 2.0))


# Process-local, visible to dispatch worker threads. Each scope owns a token so overlapping
# same-intent runs can exit in either order without restoring another run's stale value.
_scoped_intent_cap_overrides = {}
_scoped_intent_cap_lock = new_fork_reset_ledger_state(globals(), "_scoped_intent_cap_lock", threading.RLock)


@contextlib.contextmanager
def scoped_intent_cap(intent, usd):
    """Tighten the existing door ceiling for one intent (or a shared set of phase intents).

    Flow/chain receipts are opt-in and are not reliably propagated through every batch/lane
    executor. Conservatively include prior same-intent spend in the configured window instead
    of subtracting a receipt baseline that could omit paid work. No in-flight work is cancelled.
    """
    if usd is None:
        yield
        return
    intents = tuple(dict.fromkeys((intent,) if isinstance(intent, str) else (intent or ())))
    intents = tuple(label for label in intents if label)
    budget = float(usd)
    if not math.isfinite(budget) or budget < 0:
        raise ValueError("budget_usd must be finite and nonnegative")
    token = object()
    with _scoped_intent_cap_lock:
        for label in intents:
            existing = intent_cap(label)
            effective = budget if existing is None else min(budget, existing)
            _scoped_intent_cap_overrides.setdefault(label, {})[token] = (effective, budget, intents)
    try:
        yield
    finally:
        with _scoped_intent_cap_lock:
            for label in intents:
                entries = _scoped_intent_cap_overrides[label]
                del entries[token]
                if not entries:
                    del _scoped_intent_cap_overrides[label]


def scoped_intent_authorizations(intent):
    """Snapshot active (effective cap, authorized budget, shared phase intents) at the door."""
    with _scoped_intent_cap_lock:
        return tuple(_scoped_intent_cap_overrides.get(intent, {}).values())


def intent_cap(intent):
    """The per-intent running-$ ceiling (guardrail D, enforced at the CALL DOOR so it holds on EVERY path — a single
    call, a hand-rolled fan, or bulk_delegate), or None when none is set for this intent. Resolution: scoped → env
    $GATE_INTENT_CAP_<INTENT> (uppercased, non-alnum→_) → caps.intent_caps[intent] (a {intent: usd} map in config).
    None means no per-intent cap (the global daily/monthly + RT caps still apply). This is what turns 'budget_usd only
    inside bulk_delegate' into a control every metered caller is subject to."""
    scoped = scoped_intent_authorizations(intent)
    if scoped:
        return min(entry[0] for entry in scoped)
    if not intent:
        return None
    env = os.getenv("GATE_INTENT_CAP_" + "".join(c if c.isalnum() else "_" for c in intent).upper())
    if env is not None:
        try:
            return float(env)
        except ValueError:
            return None
    v = (_cfg_get("caps", "intent_caps", {}) or {}).get(intent)
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def intent_cap_window_s():
    """The rolling window (seconds) over which the per-intent running cap sums ACTUAL spend. Default 24h.
    env $GATE_INTENT_CAP_WINDOW_S → caps.intent_cap_window_s → 86400."""
    v = os.getenv("GATE_INTENT_CAP_WINDOW_S")
    try:
        return int(v) if v is not None else int(_cfg_get("caps", "intent_cap_window_s", 86400))
    except (TypeError, ValueError):
        return 86400


def external_cap():
    """Separate daily $ cap for non-token EXTERNAL spend (MCP/tool calls + external paid APIs). None = NO cap
    (opt-in — external gating does nothing until a cap is set). env GATE_EXTERNAL_DAILY → config.json
    caps.external.daily → None."""
    v = os.getenv("GATE_EXTERNAL_DAILY")
    if v is None:
        v = _cfg_get("caps", "external.daily", None)
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def advisor_model():
    """Model for the advisor's REASONING (insight synthesis + `optimize`). Realtime; capped by caps.meta.
    Configurable: env SPENDGUARD_ADVISOR_MODEL > config.json advisor.model > default (Opus 4.8)."""
    return os.getenv("SPENDGUARD_ADVISOR_MODEL") or _cfg_get("advisor", "model", "claude-opus-4-8")


def advisor_judge_model():
    """Model for BULK quality reconstruction / judging. Batch API; capped by caps.meta.
    Configurable: env SPENDGUARD_ADVISOR_JUDGE_MODEL > config.json advisor.judge_model > default (Haiku 4.5)."""
    return os.getenv("SPENDGUARD_ADVISOR_JUDGE_MODEL") or _cfg_get("advisor", "judge_model", "claude-haiku-4-5")


def advisor_adjudicator_model():
    """Model for the OPUS-tier ADJUDICATOR in the two-tier requirement judge: the screen (advisor_judge_model, Haiku)
    rules the CLEAR calls, and this stronger model rules only the ones the screen flags as NOT confident. Realtime,
    capped by caps.meta. Configurable: env SPENDGUARD_ADVISOR_ADJUDICATOR_MODEL > config.json advisor.adjudicator_model
    > default (Opus 4.8)."""
    return os.getenv("SPENDGUARD_ADVISOR_ADJUDICATOR_MODEL") or _cfg_get("advisor", "adjudicator_model", "claude-opus-4-8")


def recall_model():
    """Model for the AGENTIC RECALL pass (conv.classify_evidence — "is this chunk spend evidence / a cost lesson?").
    A high-volume, simple yes/no classification over the whole corpus, so default to the CHEAPEST capable model
    (gpt-5-nano, $0.05/1M in) — whole-corpus recall lands <10c (~free), which is what lets it replace the keyword
    pre-filters everywhere (incl. the old 'free' index stages) instead of preserving them. Capped by caps.meta.
    Configurable: env SPENDGUARD_RECALL_MODEL > config.json advisor.recall_model > default (gpt-5-nano)."""
    return os.getenv("SPENDGUARD_RECALL_MODEL") or _cfg_get("advisor", "recall_model", "gpt-5-nano")


def budget_backend():
    return _cfg_get("budget", "backend", "memory")


LEDGER_HOME_ENVIRONMENT_KEY = "SPENDGUARD_HOME"
LEDGER_HOME_EXPANSION_ENVIRONMENT_KEY = "HOME"


def active_spendguard_home():
    """Return the live configured state home, including post-import test isolation."""
    return Path(os.environ.get(LEDGER_HOME_ENVIRONMENT_KEY) or HOME)


def lane_queue_db_path():
    """The lane_queue's OWN database file — deliberately NOT the spend.db money ledger. The queue is transient work
    state (leased/parked/done/failed task rows with large task/result TEXT), not money-of-record; co-locating it in
    spend.db made it 63% of a 4.4GB ledger (2.76GB of queue rows), so every drain's purge ran a multi-GB scan under
    the ledger's write lock, every pre-reconcile snapshot and B2 backup copied 2.76GB of transient data, and the WAL
    could never find a quiet moment to checkpoint (measured 2026-10-08). Its own file isolates all of that from the
    ledger. Config budget.lane_queue_db_path overrides; default HOME/lane_queue.db."""
    p = _cfg_get("budget", "lane_queue_db_path", None)
    return p if p else str(HOME / "lane_queue.db")


@contextlib.contextmanager
def single_instance_lock(name):
    """NON-BLOCKING cross-process single-instance (mutex / count-1 semaphore) guard, held for the `with` body.

    flock(LOCK_EX|LOCK_NB) on HOME/<name>.lock: the FIRST process into the body holds it and yields True (proceed);
    a SECOND process finds it held and yields False (do NOT start a duplicate — the caller bails). The OS drops the
    lock when this process exits (flock is bound to the fd), so a crash leaves NO stale lock to reap. Fails closed
    and loudly when locking is unavailable or the lockfile cannot be opened: proceeding without exclusion could
    create two owners while falsely reporting singleton protection.

    This is the SSOT for process-singleton locking (e.g. `spendguard deploy`, whose two concurrent gate runs each
    spawn a full test suite and starve each other's CPU → the flaky-timeout failures). lane_queue's own
    `_acquire_drain_lock` is the same mechanism specialised to the drain; this is the general, reusable form."""
    try:
        import fcntl as _fcntl
    except ImportError as error:
        raise SingleInstanceLockUnavailableError("cross-process singleton locking requires fcntl.flock") from error
    try:
        fd = os.open(str(HOME / f"{name}.lock"), os.O_CREAT | os.O_RDWR, 0o644)
    except OSError as error:
        raise SingleInstanceLockUnavailableError(
            f"cannot open singleton lockfile under configured spendguard home: {HOME}"
        ) from error
    try:
        _fcntl.flock(fd, _fcntl.LOCK_EX | _fcntl.LOCK_NB)
    except OSError:
        try:
            os.close(fd)
        except OSError:
            pass
        yield False                                          # another instance holds it → caller must not start a 2nd
        return
    try:
        yield True
    finally:
        try:
            os.close(fd)                                     # closing the fd drops the flock, even on an exception
        except OSError:
            pass


def ssl_context():
    """SSL context that works under bare venvs too (urllib otherwise can't find CA certs on macOS)."""
    import ssl
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return ssl.create_default_context()


def email_config():
    """SMTP/recipient config from ~/.spendguard/email.json, overlaid by env. Secrets stay here
    (gitignored) or in env — never in the repo."""
    import json as _json
    cfg = {}
    p = HOME / "email.json"
    try:
        if p.exists():
            cfg.update(_json.loads(p.read_text()))
    except Exception:
        pass
    for key, env in (("host", "SPENDGUARD_SMTP_HOST"), ("port", "SPENDGUARD_SMTP_PORT"),
                     ("user", "SPENDGUARD_SMTP_USER"), ("password", "SPENDGUARD_SMTP_PASS"),
                     ("from_", "SPENDGUARD_EMAIL_FROM"), ("to", "SPENDGUARD_EMAIL_TO"),
                     ("provider", "SPENDGUARD_EMAIL_PROVIDER"), ("api_key", "SPENDGUARD_RESEND_KEY")):
        v = os.environ.get(env)
        if v:
            cfg[key] = v
    return cfg


def _project_saas():
    """Repo-local SaaS overlay: nearest `.spendguard.json` found walking up from CWD (stop at $HOME / fs root).
    Lets DIFFERENT repos on one machine push to different orgs/teams (e.g. nlp-pipeline→Acme/NLP, vision-pipeline→its org).
    Keep it gitignored — it holds the org/team api_key. Overlays the global saas.json; env still wins."""
    import json as _json
    try:
        d = Path.cwd().resolve()
    except Exception:
        return {}
    home = Path.home().resolve()
    for _ in range(40):
        p = d / ".spendguard.json"
        try:
            if p.exists():
                return _json.loads(p.read_text())
        except Exception:
            return {}
        if d == home or d.parent == d:
            break
        d = d.parent
    return {}


def saas_config():
    """SaaS / team roll-up connection. Precedence: global ~/.spendguard/saas.json < repo-local .spendguard.json
    (so each repo can target its own org/team) < env. Secrets (api_key) stay in those gitignored files or env —
    never the repo source. The key is the identity: the server maps it to user/team/org.
    Returns: enabled(bool), url, api_key, visibility, sync_interval, contributor."""
    import json as _json
    cfg = {}
    p = HOME / "saas.json"
    try:
        if p.exists():
            cfg.update(_json.loads(p.read_text()))
    except Exception:
        pass
    for k, v in _project_saas().items():       # repo-local overlay wins over the global config
        if v is not None and v != "":
            cfg[k] = v
    for key, env in (("enabled", "SPENDGUARD_SAAS"), ("url", "SPENDGUARD_SAAS_URL"),
                     ("api_key", "SPENDGUARD_SAAS_KEY"), ("visibility", "SPENDGUARD_VISIBILITY"),
                     ("sync_interval", "SPENDGUARD_SYNC_INTERVAL"), ("contributor", "SPENDGUARD_CONTRIBUTOR"),
                     ("project", "SPENDGUARD_PROJECT")):
        v = os.environ.get(env)
        if v is not None and v != "":
            cfg[key] = v
    cfg["enabled"] = str(cfg.get("enabled", "")).lower() in ("1", "true", "yes", "y")
    cfg.setdefault("visibility", "private")
    cfg.setdefault("sync_interval", "daily")
    cfg.setdefault("url", "https://llmspendguard.com")   # the only roll-up destination (the hosted aggregator)
    return cfg


def identity_path(): return HOME / "identity.json"


def machine_id():
    """Stable, persisted anonymous contributor id (`usr_<hex>`) for this user/machine — the fallback identity when
    no email is set, so spend is NEVER unattributed and per-user roll-up + billing always have someone to count.
    Generated once, written to ~/.spendguard/identity.json, reused forever. (Replaces the old user@host fallback,
    which leaked the OS username + wasn't a stable id.)"""
    import json as _json
    p = identity_path()
    try:
        if p.exists():
            v = (_json.loads(p.read_text()).get("contributor") or "").strip()
            if v:
                return v
    except Exception:
        pass
    import uuid
    v = "usr_" + uuid.uuid4().hex[:12]
    try:
        HOME.mkdir(parents=True, exist_ok=True)
        p.write_text(_json.dumps({"contributor": v}))
    except Exception:
        pass
    return v


def is_email(s):
    """True if the contributor string is an email (→ it can double as the alert target). Else it's an anonymous id."""
    import re
    return bool(re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", (s or "").strip()))


def saas_path(): return HOME / "saas.json"
def saas_state_path(): return HOME / "saas_state.json"   # last_sync timestamp (not the config; written each sync)


def disabled(): return os.getenv("GATE_DISABLE") == "1" or FLAG.exists()
def allow():    return os.getenv("GATE_ALLOW") == "1"


def api_get(url, headers, timeout=90):
    """Authenticated GET → parsed JSON. The ONE place an HTTP read to a provider happens.

    There were three: resources._get (timeout, context manager, SSL context — correct), report._paged
    (timeout + context manager, NO SSL context) and reconcile_anthropic._get, which passed NO TIMEOUT and
    returned the raw response object for the caller to json.load. That last one is how a provider stall
    becomes a hung reconcile with no error and no output: urlopen without a timeout waits forever, and the
    socket was never closed either, so a paged walk leaked one connection per page.
    """
    import json as _json
    return _json.loads(api_get_text(url, headers, timeout))


def api_get_text(url, headers, timeout=90):
    """Authenticated GET → decoded body text. Same single transport as api_get; separate because a batch
    RESULTS url serves JSONL, not JSON, and parsing it as JSON would fail on the second line. Two content
    types, one place that opens a socket."""
    import urllib.request
    req = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(req, timeout=timeout, context=ssl_context()) as r:
        return r.read().decode()

import json as _json_mod


def keep_backups_default():
    """Timestamped copies retained per rewritten JSON file (config `safety.keep_backups`, env
    SPENDGUARD_KEEP_BACKUPS). The `<file>~` previous-version backup is unconditional and not counted here."""
    import os as _os
    v = _os.environ.get("SPENDGUARD_KEEP_BACKUPS")
    if v is None:
        v = _cfg_get("safety", "keep_backups", 3)
    try:
        return max(0, int(v))
    except (TypeError, ValueError):
        return 3


def keep_corrupt_default():
    """Quarantined `<file>.corrupt.<stamp>` copies retained PER BASE FILE (config `safety.corrupt_keep`, env
    SPENDGUARD_CORRUPT_KEEP). These are REBUILDABLE caches moved aside on a parse failure (update_json) and kept
    for forensics — but bounded: 142 `resource_state_state.json.corrupt.*` copies had accumulated unpruned
    (2026-10-09). Env wins so a corrupt config.json can never wedge the bound; 0 disables pruning."""
    import os as _os
    v = _os.environ.get("SPENDGUARD_CORRUPT_KEEP")
    if v is None:
        v = _cfg_get("safety", "corrupt_keep", 3)
    try:
        return max(0, int(v))
    except (TypeError, ValueError):
        return 3


def state_path(name):
    """Path of an adapter's state file under HOME. One naming rule, so `chat`, `claudecode`, `codex`,
    `realized` and the rest cannot drift apart on where they keep it."""
    return HOME / f"{name}_state.json"


def load_state(name, default=None):
    """An adapter's persisted state, or `default` if it is missing OR unreadable. Distinguishing those two
    is the caller's business — save_state refuses to overwrite a file it could not read."""
    try:
        return _json_mod.loads(state_path(name).read_text())
    except Exception:
        return dict(default) if isinstance(default, dict) else (default if default is not None else {})


def save_state(name, obj, loud=True):
    """Persist an adapter's state ATOMICALLY. Returns True on success.

    THERE WERE FIVE COPIES OF THIS, three of them byte-identical:

        try:
            config.HOME.mkdir(parents=True, exist_ok=True)
            _state_path().write_text(json.dumps(st, indent=0))
        except Exception:
            pass

    Both halves are wrong. `write_text` is not atomic, so a crash or a full disk mid-write leaves a
    TRUNCATED state file — and these files are read back with a bare `except: return {}`, so the truncation
    presents as "no state" rather than as damage. And `except: pass` means a save that never happened looks
    exactly like one that did. For claudecode that state holds `counted_ids`, the set that stops resume and
    branch replays from counting the same message.id repeatedly; losing it silently re-inflates est-value by
    roughly 2.4x, and nothing anywhere says so.
    """
    try:
        HOME.mkdir(parents=True, exist_ok=True)
        return update_json(state_path(name), lambda d: obj, reason=f"{name} state",
                           quarantine_unparseable=True) is not None
    except Exception as e:
        if loud:
            import sys as _sys
            _sys.stderr.write(f"  ⚠ could not save {name} state ({type(e).__name__}: {str(e)[:70]}). The next "
                              f"run will re-read from the last saved watermark, which may double-count.\n")
        return False


def uid(n=16):
    """A short random identifier. callio and learn each carried a byte-identical private copy of this."""
    import uuid
    return uuid.uuid4().hex[:n]


def content_hash(data, length):
    """The ONE content-hash primitive: a stable, truncated SHA-256 hex id of `data` (already-encoded bytes).

    Callers pass BYTES so each keeps its OWN normalization (conv._chash: raw UTF-8 of the text; measurement._meas_sha/
    item_id: \\x00-domain-separated, str()-coerced, errors='replace') — those byte encodings are NOT interchangeable, so
    the shared part is only the sha256+truncate, not the encoding. `length` = hex chars kept, and it is the caller's
    EXPLICIT, fixed choice: changing it RE-KEYS every id built at that length (conv segment hashes, measurement sample/
    reading ids), so check every reader before changing a length. Replaces the two independent truncated-sha256
    implementations conv._chash (20) and measurement.item_id (16) once shared."""
    import hashlib
    return hashlib.sha256(data).hexdigest()[:length]


_WARNED_ONCE = set()
CODEX_LEASE_ACTIVE_ENV = "SPENDGUARD_CODEX_LEASE_ACTIVE"


def warn_once(msg, prefix=""):
    """Print a warning the FIRST time this exact message appears, then never again.

    deid and gate each had their own copy plus their own `_WARNED` set. Two sets means a message crossing
    between them warns twice, and — worse — the reason this pattern exists at all is that an 800-request
    batch warning 800 times is a warning nobody reads. One registry, one line per distinct problem."""
    import sys as _sys
    line = (prefix + msg) if prefix else msg
    if line not in _WARNED_ONCE:
        _WARNED_ONCE.add(line)
        _sys.stderr.write(line + "\n")


def project_of_cwd(cwd, default):
    """Bucket a session cwd to its REPO (git-root basename) so subdirs collapse to the repo and match how
    actual-$ is tagged, instead of fragmenting est-value across dozens of cwd names. `default` is the
    adapter's own fallback label — the ONLY thing that differed between claudecode's and codex's copies."""
    import os as _os
    if not cwd:
        return default
    return git_root_project(cwd) or _os.path.basename(str(cwd).rstrip("/")).lower() or default


# Keep a filesystem reserve before bulk writes. Bytes only: no ambiguous size suffixes.
DISK_GUARD_FLOOR_DEFAULT_BYTES = 5 * 1024 ** 3
DISK_GUARD_FLOOR_ENV = "SPENDGUARD_DISK_GUARD_FLOOR_BYTES"


def disk_guard_floor_bytes():
    """Positive integer reserve; environment overrides config.json disk_guard.floor_bytes."""
    value = os.environ.get(DISK_GUARD_FLOOR_ENV)
    if value is None:
        value = _cfg_get("disk_guard", "floor_bytes", DISK_GUARD_FLOOR_DEFAULT_BYTES)
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise ValueError("disk_guard.floor_bytes must be a positive integer byte count")
    floor = int(value)
    if floor <= 0:
        raise ValueError("disk_guard.floor_bytes must be a positive integer byte count")
    return floor


SPEND_DAILY_ROLLUP_WRITE_TIMEOUT_S = 0.0


def spend_daily_rollup_reporting_timezone():
    """Accounting-day timezone shared with ledger event normalization."""
    return os.environ.get("SPENDGUARD_REPORTING_TZ") or "UTC"


SPEND_DAILY_ROLLUP_AUDIT_INTERVAL_DEFAULT_SECONDS = 3600


def spend_daily_rollup_audit_interval_seconds():
    """Positive maintenance cadence from environment or daily_rollup config."""
    value = os.environ.get("SPENDGUARD_DAILY_ROLLUP_AUDIT_INTERVAL_SECONDS")
    if value is None:
        value = _cfg_get("daily_rollup", "audit_interval_seconds",
                         SPEND_DAILY_ROLLUP_AUDIT_INTERVAL_DEFAULT_SECONDS)
    interval = float(value)
    if not math.isfinite(interval) or interval <= 0:
        raise ValueError("daily_rollup.audit_interval_seconds must be positive and finite")
    return interval
