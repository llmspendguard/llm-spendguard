"""Cross-process spend ledger (SQLite, WAL) for fleet-wide DAILY / MONTHLY caps — no proxy.

Enabled by config `budget.backend = sqlite`. The gate records every charge here and checks cumulative
spend across ALL processes before allowing more. Default `backend = memory` keeps the per-process
real-time cap only (this module is then never touched). Per-call SQLite I/O is fine for moderate
real-time volume; very high-volume loops should stay on the in-process cap.
"""
import sqlite3, datetime, threading
from . import config

_conn = None
_lock = threading.RLock()   # reentrant: record()/spent_since() hold it AND call _db() which re-acquires


def _db():
    global _conn
    if _conn is None:
        with _lock:
            if _conn is None:
                c = sqlite3.connect(config.db_path(), timeout=10, check_same_thread=False)
                c.execute("PRAGMA journal_mode=WAL")
                c.execute("CREATE TABLE IF NOT EXISTS charges "
                          "(ts TEXT, day TEXT, provider TEXT, model TEXT, kind TEXT, cost REAL, project TEXT DEFAULT '')")
                cols = [r[1] for r in c.execute("PRAGMA table_info(charges)").fetchall()]
                if "project" not in cols:                      # migrate older ledgers
                    c.execute("ALTER TABLE charges ADD COLUMN project TEXT DEFAULT ''")
                c.execute("CREATE INDEX IF NOT EXISTS idx_day ON charges(day)")
                c.commit()
                _conn = c
    return _conn


_PROJECT = None


def _project():
    """Project tag for a charge (the repo/work this spend belongs to) — cached per process. Order:
    $SPENDGUARD_PROJECT → saas config `project` (repo-local .spendguard.json) → git repo root basename → cwd."""
    global _PROJECT
    if _PROJECT is not None:
        return _PROJECT
    import os
    v = os.environ.get("SPENDGUARD_PROJECT")
    if not v:
        try:
            v = config.saas_config().get("project")
        except Exception:
            v = None
    if not v:
        try:
            import subprocess
            root = subprocess.run(["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True, timeout=2).stdout.strip()
            if root:
                v = os.path.basename(root)
        except Exception:
            pass
    if not v:
        try:
            v = os.path.basename(os.getcwd())
        except Exception:
            v = ""
    _PROJECT = (v or "").strip().lower()[:64]
    return _PROJECT


def record(provider, model, kind, cost, project=None):
    if not cost:
        return
    proj = project if project is not None else _project()
    now = datetime.datetime.now(datetime.timezone.utc)
    with _lock:
        _db().execute("INSERT INTO charges (ts,day,provider,model,kind,cost,project) VALUES (?,?,?,?,?,?,?)",
                      (now.isoformat(timespec="seconds"), now.strftime("%Y-%m-%d"),
                       provider or "?", model or "?", kind, float(cost), proj or ""))
        _db().commit()


def spent_since(day):  # WORKLOAD spend only — excludes spendguard's own meta AND reconciled (historical) rows
    with _lock:
        r = _db().execute("SELECT COALESCE(SUM(cost),0) FROM charges WHERE day >= ? "
                          "AND (kind IS NULL OR kind != 'meta') "
                          "AND (project IS NULL OR project <> 'unattributed')", (day,)).fetchone()
    return float(r[0] or 0)


# ── reconciliation: make the LOCAL ledger reflect PROVIDER-billed truth (the gap = ungoverned/pre-ledger spend) ──
def by_provider_day(kind=None, since=None):
    """{(provider, day): $} of GATE-recorded spend (excludes reconciled rows) — the attributed side of reconcile."""
    cond, args = ["(project IS NULL OR project <> 'unattributed')"], []
    if kind:
        cond.append("kind=?"); args.append(kind)
    if since:
        cond.append("day >= ?"); args.append(since)
    where = "WHERE " + " AND ".join(cond)
    with _lock:
        rows = _db().execute(f"SELECT COALESCE(provider,'?'), day, COALESCE(SUM(cost),0) FROM charges {where} "
                             f"GROUP BY provider, day", args).fetchall()
    return {(p, d): float(c or 0) for p, d, c in rows}


def record_reconciled(day, provider, cost):
    """Insert a reconciliation row for provider-billed spend we couldn't attribute (project='unattributed' →
    pushed with no contributor; excluded from the cap)."""
    with _lock:
        _db().execute("INSERT INTO charges (ts,day,provider,model,kind,cost,project) VALUES (?,?,?,?,?,?,?)",
                      (day + "T00:00:00+00:00", day, provider or "?", "(provider-batch)", "batch", float(cost), "unattributed"))
        _db().commit()


def clear_reconciled(since=None):
    """Remove prior reconciliation rows so reconcile is idempotent (rebuilds them)."""
    with _lock:
        if since:
            _db().execute("DELETE FROM charges WHERE project='unattributed' AND day >= ?", (since,))
        else:
            _db().execute("DELETE FROM charges WHERE project='unattributed'")
        _db().commit()


# ── spendguard's own advisor LLM use (segregated: own cap, own line, excluded from workload) ──
def record_meta(provider, model, cost):
    record(provider, model, "meta", cost, project="llmseg")   # spendguard's own spend → its own project tag


def meta_spent_since(day):
    with _lock:
        r = _db().execute("SELECT COALESCE(SUM(cost),0) FROM charges WHERE day >= ? AND kind='meta'",
                          (day,)).fetchone()
    return float(r[0] or 0)


def meta_spent_today():
    return meta_spent_since(_utc().strftime("%Y-%m-%d"))


def meta_exceeded(pending=0.0):
    cap = config.meta_cap()
    if cap is not None and meta_spent_today() + pending > cap:
        return ("meta", cap, meta_spent_today() + pending)
    return None


def by_day(kind=None, exclude_meta=False, since=None):
    """{day: total$} from the local ledger, optionally filtered by kind / excluding meta / since a date."""
    cond, args = [], []
    if kind:
        cond.append("kind=?"); args.append(kind)
    if exclude_meta:
        cond.append("(kind IS NULL OR kind != 'meta')")
    if since:
        cond.append("day >= ?"); args.append(since)
    where = ("WHERE " + " AND ".join(cond)) if cond else ""
    with _lock:
        rows = _db().execute(f"SELECT day, COALESCE(SUM(cost),0) FROM charges {where} GROUP BY day", args).fetchall()
    return {d: float(v or 0) for d, v in rows}


def by_dims(since=None):
    """Per-day rows grouped by (day, provider, model, kind) for the SaaS roll-up push — the structured shape
    the server's /v1/ledger expects (vs by_day's flat {day: $}). Returns dicts with cost in $ and a call count."""
    cond, args = [], []
    if since:
        cond.append("day >= ?"); args.append(since)
    where = ("WHERE " + " AND ".join(cond)) if cond else ""
    with _lock:
        rows = _db().execute(
            f"SELECT day, COALESCE(provider,'?'), COALESCE(model,'?'), COALESCE(kind,'workload'), "
            f"COALESCE(project,''), COALESCE(SUM(cost),0), COUNT(*) FROM charges {where} "
            f"GROUP BY day, provider, model, kind, project", args
        ).fetchall()
    return [dict(day=d, provider=p, model=m, kind=k, project=pr, cost=float(c or 0), calls=int(n)) for d, p, m, k, pr, c, n in rows]


def ledger_start():
    """Earliest day in the local ledger — spend before this wasn't recorded locally (pre-ledger)."""
    with _lock:
        r = _db().execute("SELECT MIN(day) FROM charges").fetchone()
    return r[0] if r and r[0] else None


def _utc():
    return datetime.datetime.now(datetime.timezone.utc)


def spent_today():  return spent_since(_utc().strftime("%Y-%m-%d"))
def spent_month():  return spent_since(_utc().strftime("%Y-%m-01"))


def exceeded(pending=0.0):
    """(window, cap, projected) if a configured daily/monthly cap would be exceeded, else None."""
    d = config.daily_cap()
    if d is not None and spent_today() + pending > d:
        return ("daily", d, spent_today() + pending)
    m = config.monthly_cap()
    if m is not None and spent_month() + pending > m:
        return ("monthly", m, spent_month() + pending)
    return None
