"""The GREEN POINTER: which committed, gate-passing commit the fleet should be serving, and whether THIS
process is behind it.

WHY THIS EXISTS. spendguard's MCP surface runs as a stdio subprocess spawned per Claude session, and the
package is an EDITABLE install — so a FRESH process reads the working tree directly, but a LONG-RUNNING one is
frozen at the code it imported when it spawned. Commit new code and every already-running server keeps serving
the OLD code until it is restarted, with nothing to even show which processes are stale. That is a real
incident, not a hypothetical: seven servers were found each pinned to a different commit, distinguishable only
by reading their process start-times.

THE MODEL (blue/green, reshaped for per-session stdio). A deploy promotes the current git HEAD to the green
pointer at `config.HOME/current_release.json` — but ONLY when the working tree is clean (the code is committed)
and the full gate passes, so "green" (blue/green) == "green" (suite + ruff + name gate). That is the "best" in
"always the latest with the best": the pointer never advances to uncommitted or failing code. Each server
records the SHA it started from; when the pointer moves ahead of it, `stale_vs_green()` is True and the server
hands off to a fresh process at a SAFE point — between requests, nothing in flight (see mcp_server.serve_stdio).

Nothing here decides MEANING — it is a git-SHA comparison and an atomic file write — so there is no LLM and no
regex-as-judgement. The served identity is captured ONCE (the code THIS process is running); the pointer is
read live. On any failure (not a git tree, git missing, unreadable pointer) every accessor degrades to
"unknown", which reads as NOT stale — a server whose version cannot be established keeps running rather than
thrashing.
"""
import datetime
import functools
import os
import pathlib
import subprocess

from . import config

# The green pointer lives in the state home (SPENDGUARD_HOME), beside the ledger and caches — resolved live so a
# test's isolated home is honoured. NOT cached at import: the home can be set after this module loads.
POINTER_NAME = "current_release.json"


def pointer_path():
    return pathlib.Path(config.HOME) / POINTER_NAME


def _src_root():
    """The directory the package's code is imported FROM (…/src/spendguard). git run here targets the spendguard
    repo whether or not the process's CWD is inside it."""
    return pathlib.Path(__file__).resolve().parent


def _git_src(*args):
    """Run `git <args>` in the src tree; stdout stripped, or None on ANY failure (git missing, not a repo,
    timeout). Never raises — a version probe must not break a caller."""
    try:
        p = subprocess.run(["git", "-C", str(_src_root()), *args],
                           capture_output=True, text=True, timeout=3)
    except Exception:
        return None
    if p.returncode != 0:
        return None
    return p.stdout.strip()


def _tracked_dirty():
    """True iff a TRACKED file has an uncommitted change (porcelain line not starting with '??'). Untracked-only
    (scratch, ignored outputs) does NOT count as dirty — the committed code is what is served."""
    out = _git_src("status", "--porcelain")
    if not out:
        return False
    return any(not ln.startswith("??") for ln in out.splitlines() if ln.strip())


@functools.lru_cache(maxsize=1)
def served_sha():
    """The commit this process's code was imported from — captured on the FIRST call and FROZEN for the life of
    the process (memoised write-once; there is deliberately NO refresh). The editable install serves the working
    tree, so HEAD is that identity, with a `dirty` flag when tracked files were uncommitted at startup. Returns
    {sha, short, dirty, describe} or None outside a git tree. Frozen on purpose: re-reading HEAD later would
    report a checkout that moved AFTER import — code this process is NOT running — the exact confusion this
    module removes. Tests reset it with `served_sha.cache_clear()` after stubbing `_git_src`."""
    sha = _git_src("rev-parse", "HEAD")
    if not sha:
        return None
    return {"sha": sha, "short": sha[:7], "dirty": _tracked_dirty(),
            "describe": _git_src("describe", "--tags", "--always", "--dirty") or sha[:7]}


def green_pointer():
    """The current green pointer dict {sha, short, describe, ts, gate, actor}, or None if none has been
    promoted yet / it is unreadable. Read live (never memoised — it is what changes). Never raises."""
    import json
    p = pointer_path()
    try:
        if not p.exists():
            return None
        d = json.loads(p.read_text() or "{}")
        return d if isinstance(d, dict) and d.get("sha") else None
    except Exception:
        return None


def stale_vs_green():
    """Is THIS process behind the green pointer? True only when both SHAs are known AND differ — a newer green
    commit has been deployed than the one this process is running. Unknown either way (no pointer, no git) →
    False, so a server never respawns on a signal it cannot establish."""
    g = green_pointer()
    s = served_sha()
    if not g or not s:
        return False
    return g.get("sha") != s.get("sha")


def current_respawn_decision():
    """Return the total, traceable install-aware replacement decision for this process."""
    from . import __version__
    from .manifest import (MANIFEST_EDITABLE_KIND, MANIFEST_WHEEL_KIND,
                           inspect_current_interpreter_install_kind)
    install = inspect_current_interpreter_install_kind()
    if install.get("install_kind") == MANIFEST_EDITABLE_KIND:
        if not stale_vs_green():
            return {"should_respawn": False, "install_kind": MANIFEST_EDITABLE_KIND,
                    "reason": "editable process is not behind the green pointer"}
        green = green_pointer()
        head = _git_src("rev-parse", "HEAD")
        lands_on_green = bool(green and head and green.get("sha") == head)
        return {"should_respawn": lands_on_green, "install_kind": MANIFEST_EDITABLE_KIND,
                "reason": ("editable replacement loads green HEAD" if lands_on_green else
                           "editable replacement would not load the green HEAD")}
    if install.get("install_kind") == MANIFEST_WHEEL_KIND:
        installed_version = install.get("wheel_version")
        differs = bool(installed_version and installed_version != __version__)
        return {"should_respawn": differs, "install_kind": MANIFEST_WHEEL_KIND,
                "reason": ("installed wheel differs from the running package" if differs else
                           "running package already matches the installed wheel"),
                "running_version": __version__, "installed_version": installed_version}
    return {"should_respawn": False, "install_kind": install.get("install_kind", "unknown"),
            "reason": install.get("verification_error") or "install kind is unresolved"}


def should_respawn():
    """Return whether a replacement process would load newer code without creating an exit loop."""
    return current_respawn_decision()["should_respawn"]


def exit_if_stale(log=None):
    """SELF-HEAL for any long-lived spendguard consumer: if this process is running code older than the green pointer
    AND exiting would land a fresh process on green (should_respawn), EXIT so the process's supervisor (launchd
    KeepAlive, an MCP client, a run loop) respawns it onto the latest code. The one-liner that generalises the MCP
    fleet's auto-respawn to any daemon — call it periodically or between units of work. Returns False when NOT stale
    (so a caller can `if release.exit_if_stale(): ...` defensively); never returns True (it exits first). A daemon that
    is NOT supervised should NOT call this (nothing would restart it); it appears in `fleet.restart_stale`'s warn list
    instead. $0, never raises except the deliberate SystemExit."""
    try:
        if not should_respawn():
            return False
        if log:
            try:
                log("[spendguard] exit_if_stale: running %s, green is %s — exiting to respawn on the latest code"
                    % ((served_sha() or {}).get("short"), (green_pointer() or {}).get("short")))
            except Exception:
                pass
    except Exception:
        return False
    raise SystemExit(0)                               # supervisor respawns → fresh process serves the green commit


def promote_release(sha=None, *, describe=None, gate="", actor="deploy", allow_dirty=False):
    """Advance the green pointer to `sha` (default: current HEAD). Refuses a dirty working tree unless
    allow_dirty (the pointer must name COMMITTED code), and refuses a sha git cannot resolve. Does NOT run the
    gate itself — the deploy command orchestrates gate→promote and records its result in `gate`; this is the
    atomic write, through config.update_json (temp+os.replace, `~` + timestamped backups). Returns the written
    dict, or raises ValueError on refusal."""
    if sha is None:
        sha = _git_src("rev-parse", "HEAD")
    if not sha:
        raise ValueError("promote_release: no git HEAD to promote (is this a git checkout?)")
    full = _git_src("rev-parse", sha)                 # resolve/validate the ref → a full sha
    if not full:
        raise ValueError(f"promote_release: git cannot resolve {sha!r}")
    if _tracked_dirty() and not allow_dirty:
        raise ValueError("promote_release: the working tree has uncommitted changes to tracked files — commit "
                         "first so the pointer names committed code, or pass allow_dirty=True deliberately.")
    rec = {"sha": full, "short": full[:7],
           "describe": describe or _git_src("describe", "--tags", "--always") or full[:7],
           "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
           "gate": gate, "actor": actor, "host": os.uname().nodename if hasattr(os, "uname") else ""}
    config.update_json(pointer_path(), lambda _cur: rec, reason="deploy")
    return rec


def release_status():
    """One dict for the version tool / doctor / CLI: what THIS process serves, what the green pointer names, and
    whether they diverge — the $0 signal that answers 'is this server running the latest blessed code?'."""
    s = served_sha()
    g = green_pointer()
    stale = stale_vs_green()
    if g and s and stale:
        note = ("STALE: this process serves %s but the green pointer is %s — a newer commit was deployed; a "
                "fresh process will serve it (the MCP server hands off between requests)."
                % ((s or {}).get("short"), (g or {}).get("short")))
    elif g and s and not stale:
        note = "up to date: this process serves the green pointer (%s)" % (g or {}).get("short")
    elif s and not g:
        note = "no green pointer yet — this process serves %s; run `spendguard deploy` to bless it" % s.get("short")
    else:
        note = "version unknown (not a git checkout) — staleness cannot be established; the server keeps running"
    respawn = current_respawn_decision()
    return {"served": s, "green": g, "stale": stale, "respawn": respawn,
            "served_dirty": bool((s or {}).get("dirty")), "pointer_path": str(pointer_path()), "note": note}


def converge_deployed_release(*, check=False, roots=None, dry_run=False, settings=None):
    """Release completion/scheduler entry: return the full fail-closed fleet attestation."""
    from .distribution import distribute_fleet
    return distribute_fleet(check=check, roots=roots, dry_run=dry_run, settings=settings)
