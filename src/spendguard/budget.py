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
                          "(ts TEXT, day TEXT, provider TEXT, model TEXT, kind TEXT, cost REAL, "
                          "project TEXT DEFAULT '', conv_id TEXT DEFAULT '')")
                cols = [r[1] for r in c.execute("PRAGMA table_info(charges)").fetchall()]
                if "project" not in cols:                      # migrate older ledgers
                    c.execute("ALTER TABLE charges ADD COLUMN project TEXT DEFAULT ''")
                if "conv_id" not in cols:                      # conversation/chat id per call (links to the chat)
                    c.execute("ALTER TABLE charges ADD COLUMN conv_id TEXT DEFAULT ''")
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


_CONV = None


def _conv():
    """Conversation/chat id this charge belongs to — links a call back to the chat that spawned it (so per-call
    + pre/post conversation context is recoverable). Order: $SPENDGUARD_CONV / $SPENDGUARD_CHAT /
    $CLAUDE_SESSION_ID, else a stable per-process id (calls in one run share it). Cached per process."""
    global _CONV
    if _CONV is not None:
        return _CONV
    import os
    v = (os.environ.get("SPENDGUARD_CONV") or os.environ.get("SPENDGUARD_CHAT")
         or os.environ.get("CLAUDE_SESSION_ID") or "")
    if not v:
        import uuid
        v = "proc-" + uuid.uuid4().hex[:12]
    _CONV = v.strip()[:128]
    return _CONV


def record(provider, model, kind, cost, project=None, conv_id=None):
    if not cost:
        return
    proj = project if project is not None else _project()
    conv = conv_id if conv_id is not None else _conv()
    now = datetime.datetime.now(datetime.timezone.utc)
    with _lock:
        _db().execute("INSERT INTO charges (ts,day,provider,model,kind,cost,project,conv_id) VALUES (?,?,?,?,?,?,?,?)",
                      (now.isoformat(timespec="seconds"), now.strftime("%Y-%m-%d"),
                       provider or "?", model or "?", kind, float(cost), proj or "", conv or ""))
        _db().commit()


_RECONCILED = "(provider-batch)"   # marker model for reconciliation rows (the provider-truth gap), any project


def spent_since(day):  # WORKLOAD spend only — excludes spendguard's own meta AND reconciled (historical) rows
    with _lock:
        r = _db().execute("SELECT COALESCE(SUM(cost),0) FROM charges WHERE day >= ? "
                          "AND (kind IS NULL OR kind != 'meta') "
                          "AND (model IS NULL OR model <> ?)", (day, _RECONCILED)).fetchone()
    return float(r[0] or 0)


# ── reconciliation: make the LOCAL ledger reflect PROVIDER-billed truth (the gap = ungoverned/pre-ledger spend) ──
def by_provider_day(kind=None, since=None):
    """{(provider, day): $} of GATE-recorded spend (excludes reconciled rows) — the attributed side of reconcile."""
    cond, args = ["(model IS NULL OR model <> ?)"], [_RECONCILED]
    if kind:
        cond.append("kind=?"); args.append(kind)
    if since:
        cond.append("day >= ?"); args.append(since)
    where = "WHERE " + " AND ".join(cond)
    with _lock:
        rows = _db().execute(f"SELECT COALESCE(provider,'?'), day, COALESCE(SUM(cost),0) FROM charges {where} "
                             f"GROUP BY provider, day", args).fetchall()
    return {(p, d): float(c or 0) for p, d, c in rows}


def gate_by_project_day(kind=None, since=None):
    """{(project, day): $} of GATE-recorded (attributed) spend — excludes reconciled rows. Used to compute the
    per-project gap so the provider-truth gap is attributed by evidence, not dumped in one 'unattributed' bucket."""
    cond, args = ["(model IS NULL OR model <> ?)"], [_RECONCILED]
    if kind:
        cond.append("kind=?"); args.append(kind)
    if since:
        cond.append("day >= ?"); args.append(since)
    where = "WHERE " + " AND ".join(cond)
    with _lock:
        rows = _db().execute(f"SELECT COALESCE(NULLIF(project,''),'unattributed'), day, COALESCE(SUM(cost),0) "
                             f"FROM charges {where} GROUP BY 1, day", args).fetchall()
    return {(p, d): float(c or 0) for p, d, c in rows}


def record_reconciled(day, provider, cost, project="unattributed", kind="batch", model=None):
    """Insert a reconciliation row for provider-billed (batch) OR gate-logged (realtime) spend — the gap — attributed
    to `project` by evidence ('unattributed' only when there's none). Marked by a marker model so it's excluded from
    gate/cap and rebuilt idempotently. Default marker '(provider-batch)' / kind 'batch'; the realtime backfill passes
    its own marker + kind='realtime'."""
    with _lock:
        _db().execute("INSERT INTO charges (ts,day,provider,model,kind,cost,project) VALUES (?,?,?,?,?,?,?)",
                      (day + "T00:00:00+00:00", day, provider or "?", model or _RECONCILED, kind, float(cost), project or "unattributed"))
        _db().commit()


def clear_reconciled(since=None, model=None):
    """Remove prior reconciliation rows so reconcile is idempotent (rebuilds them). Keyed by the marker model
    (default the batch marker; the realtime backfill passes its own)."""
    marker = model or _RECONCILED
    with _lock:
        if since:
            _db().execute("DELETE FROM charges WHERE model=? AND day >= ?", (marker, since))
        else:
            _db().execute("DELETE FROM charges WHERE model=?", (marker,))
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


def by_day(kind=None, exclude_meta=False, since=None, exclude_reconciled=False):
    """{day: total$} from the local ledger, optionally filtered by kind / excluding meta / excluding reconciled
    (provider-truth) rows / since a date. exclude_reconciled is essential for the LEAK check: reconciled rows ARE
    the provider truth, so counting them as 'local gate-recorded' would make coverage exceed 100%."""
    cond, args = [], []
    if kind:
        cond.append("kind=?"); args.append(kind)
    if exclude_meta:
        cond.append("(kind IS NULL OR kind != 'meta')")
    if exclude_reconciled:
        cond.append("(model IS NULL OR model <> ?)"); args.append(_RECONCILED)
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


def exceeded(pending=0.0, kind="llm"):
    """(scope, cap, projected) if a cap would be exceeded by `pending` more $ on a call of resource class
    `kind` (llm|compute), else None. Checks the class SUB-CAP then the TOTAL ceiling, daily then monthly.
    The gate governs LLM calls, so it passes kind='llm'; remote-compute caps are checked in resources.py
    (vast.ai launches don't hit the gate). meta is separate (meta_exceeded). NOTE: this local ledger holds LLM
    spend, so the total ceiling here is evaluated against LLM spend — the true LLM+compute total is composed on
    the dashboard/report; the compute portion is enforced/alerted via resources.compute_exceeded()."""
    sd, sm = spent_today(), spent_month()
    checks = []
    if kind in ("llm", "compute"):
        checks.append((f"{kind}-daily", config.class_cap(kind, "daily"), sd))
        checks.append((f"{kind}-monthly", config.class_cap(kind, "monthly"), sm))
    checks.append(("total-daily", config.class_cap("total", "daily"), sd))
    checks.append(("total-monthly", config.class_cap("total", "monthly"), sm))
    for scope, capv, sp in checks:
        if capv is not None and sp + pending > capv:
            return (scope, capv, sp + pending)
    return None
