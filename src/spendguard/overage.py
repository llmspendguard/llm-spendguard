"""Shared paid-overage status used by both CLI and MCP surfaces."""
import contextlib
import io
import json
import os
import threading
import time

from . import claudecode, config

# In-process TTL memo. The underlying probe (_compute_overage_status → claudecode._overage_events) re-reads EVERY
# claude-code transcript on each call (~10s on a large history), and it sits on plan_admission's hot path: a
# subscription-lane call that assesses shed/fallback calls current_overage_status() for the subagent_host lane. The
# weekly/5-hour overage windows it reports change on the order of minutes, never per call, so a short TTL cache makes
# every repeated call instant WITHOUT changing the verdict (a flip can lag by at most the TTL — immaterial to a
# quota-preservation shed decision). Disk-backed too, so a FRESH process (e.g. a `spendguard delegate` CLI one-shot)
# does not re-pay the full scan every invocation. The memo write is lock-guarded + MONOTONIC (a slower thread that
# computed an OLDER snapshot can never clobber a newer one).
_MEMO_LOCK = threading.Lock()
_STATUS_MEMO = {"t": 0.0, "val": None}


def _status_ttl_s():
    """Seconds a cached overage status stays fresh (config plan_admission.overage_status_ttl_s, default 60)."""
    try:
        return float(config._cfg_get("plan_admission", "overage_status_ttl_s", 60.0) or 60.0)
    except Exception:
        return 60.0


def _status_cache_path():
    return os.path.join(os.path.dirname(config.db_path()), "overage_status_cache.json")


def _memo_get(ttl, now):
    """The in-process memo if still fresh, as (computed_at, status); else None. Lock-guarded read."""
    with _MEMO_LOCK:
        if _STATUS_MEMO["val"] is not None and (now - _STATUS_MEMO["t"]) < ttl:
            return _STATUS_MEMO["t"], _STATUS_MEMO["val"]
    return None


def _memo_put(computed_at, status):
    """Store into the memo only if `computed_at` is at least as new as what's there — a slower thread that finished
    an OLDER snapshot must not overwrite a newer one. Lock-guarded so the check-and-set is atomic."""
    with _MEMO_LOCK:
        if computed_at >= _STATUS_MEMO["t"]:
            _STATUS_MEMO.update(t=float(computed_at), val=status)


def _stamp(base, computed_at, cached):
    """Return a COPY of `base` carrying its own freshness provenance, so a TTL-served value is never a SILENT skip:
    `as_of` (when the probe actually ran), `age_s` (how stale this answer is now), and `cached` (True = the ~10s scan
    was skipped under the TTL). The caller can see, in-band, that recomputation was elided and by how much."""
    return {**base, "as_of": float(computed_at), "age_s": round(max(0.0, time.time() - float(computed_at)), 1),
            "cached": bool(cached)}


def _compute_overage_status():
    """The real probe: parse the provider's own transcript signals into current windows + historical month overage.
    Read-only and ~10s on a large transcript history — callers should go through current_overage_status(), which
    caches this with a short TTL."""
    with contextlib.redirect_stdout(io.StringIO()):
        windows, _anchor = claudecode._overage_windows(claudecode._overage_events())
    now = time.time()
    con = config.open_ledger_connection(config.db_path(), tune=False)
    try:
        has_events = con.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='spend_events'").fetchone()
        month = [] if not has_events else con.execute(
            "SELECT substr(occurred_at,1,7) mo, ROUND(SUM(CAST(realtime_usd AS REAL)),2) FROM spend_events "
            "WHERE source='anthropic-invoice' AND intent LIKE 'anthropic-invoice:cc-overage%' "
            "GROUP BY mo ORDER BY mo DESC").fetchall()
    finally:
        con.close()
    return {"on_overage_now": any(begin <= now < reset for (begin, reset) in windows),
            "meaning": "true = the weekly plan cap is hit and this account is paying per-token right now",
            "observed_overage_windows": len(windows),
            "real_overage_by_month_usd": {month_name: float(dollars or 0.0) for month_name, dollars in month[:6]}}


def current_overage_status(max_age_s=None, force=False):
    """Return the observable current overage state and historical real-dollar overage by month.

    Cached with a short TTL (plan_admission.overage_status_ttl_s, default 60s) because the underlying probe re-reads
    the whole claude-code transcript tree (~10s) and the result changes only slowly. The cache is in-process AND
    disk-backed so a fresh CLI process does not re-pay the scan. `force=True` (or `max_age_s=0`) bypasses the cache
    and recomputes; `max_age_s` overrides the TTL for a caller that needs a tighter freshness bound. Every return
    carries `as_of`/`age_s`/`cached` (see _stamp), so a TTL-served value is self-describing, never a silent skip.
    Caching is bypassed entirely under SPENDGUARD_TEST_ISOLATED so tests stay hermetic (no cross-run cache bleed)."""
    isolated = bool(os.getenv("SPENDGUARD_TEST_ISOLATED"))
    ttl = (_status_ttl_s() if max_age_s is None else float(max_age_s))
    now = time.time()
    if not force and not isolated and ttl > 0:
        hit = _memo_get(ttl, now)
        if hit is not None:
            return _stamp(hit[1], hit[0], cached=True)      # in-process hit
        if os.path.exists(_status_cache_path()):            # disk hit (fresh process rides a recent CLI/daemon scan)
            try:
                with open(_status_cache_path()) as fh:
                    blob = json.load(fh)
                if (isinstance(blob, dict) and isinstance(blob.get("status"), dict)
                        and (now - float(blob.get("ts", 0.0))) < ttl):
                    _memo_put(float(blob["ts"]), blob["status"])
                    return _stamp(blob["status"], float(blob["ts"]), cached=True)
            except Exception as e:                          # a PRESENT-but-broken cache is NOT a silent miss: announce it
                config.warn_once("[spendguard] overage status disk cache unreadable (%s) — recomputing the ~10s "
                                 "transcript scan this call; the cache is not speeding fresh processes" % e)
    status = _compute_overage_status()
    _memo_put(now, status)
    if not isolated:
        # Route the whole-file write through the ONE sanctioned JSON writer (config.update_json): atomic + it refuses
        # to clobber an unparseable file. A cache is rebuildable, so quarantine_unparseable moves a corrupt file aside
        # and rewrites, and keep_backups=0 keeps only `path~` (no deep-history clutter for a cache). A declined write
        # (None) is announced, not swallowed — a cache that cannot persist leaves fresh processes re-paying the scan.
        if config.update_json(_status_cache_path(), lambda _old: {"ts": now, "status": status},
                              reason="overage status cache", quarantine_unparseable=True, keep_backups=0) is None:
            config.warn_once("[spendguard] could not write overage status cache — fresh processes will re-pay the "
                             "~10s overage scan until this is fixed")
    return _stamp(status, now, cached=False)


def overage_cli(_argv=None):
    status = current_overage_status()
    state = "YES" if status["on_overage_now"] else "no"
    print(f"paid overage now: {state} — {status['meaning']}")
    print(f"observed overage windows: {status['observed_overage_windows']}")
    if status["real_overage_by_month_usd"]:
        print("real overage $ by month: " + "  ".join(
            f"{month} ${dollars:.2f}" for month, dollars in status["real_overage_by_month_usd"].items()))
    else:
        print("real overage $ by month: none recorded")
    print("Est sub value is a separate axis and is not included in these billed overage dollars.")
    return 0
