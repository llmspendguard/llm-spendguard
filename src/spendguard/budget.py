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
                          "(ts TEXT, day TEXT, provider TEXT, model TEXT, kind TEXT, cost REAL)")
                c.execute("CREATE INDEX IF NOT EXISTS idx_day ON charges(day)")
                c.commit()
                _conn = c
    return _conn


def record(provider, model, kind, cost):
    if not cost:
        return
    now = datetime.datetime.now(datetime.timezone.utc)
    with _lock:
        _db().execute("INSERT INTO charges VALUES (?,?,?,?,?,?)",
                      (now.isoformat(timespec="seconds"), now.strftime("%Y-%m-%d"),
                       provider or "?", model or "?", kind, float(cost)))
        _db().commit()


def spent_since(day):  # WORKLOAD spend only — excludes spendguard's own meta calls
    with _lock:
        r = _db().execute("SELECT COALESCE(SUM(cost),0) FROM charges WHERE day >= ? "
                          "AND (kind IS NULL OR kind != 'meta')", (day,)).fetchone()
    return float(r[0] or 0)


# ── spendguard's own advisor LLM use (segregated: own cap, own line, excluded from workload) ──
def record_meta(provider, model, cost):
    record(provider, model, "meta", cost)


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
