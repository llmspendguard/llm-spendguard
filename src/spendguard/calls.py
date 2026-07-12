"""Rich per-call context log (opt-in) — turns spend records into a cost+QUALITY corpus.

OFF by default (it can store prompts/outputs — privacy). Enable with config calls.enabled=true
(or SPENDGUARD_CALLS=1). Per call it records: chain, intent, caller, model, cost, tokens, latency,
prompt hash (+ optional snippet), output snippet, finish reason, and a DEFERRED quality label:
  - feedback(call_id, ok)        -> explicit / judge verdict (authoritative)
  - implicit 'used'              -> a later call in the same chain reused this output
That powers cost-per-GOOD-result per intent (`spendguard calls`) and, later, an `optimize` loop.

Shares the SQLite db with budget (config.db_path()), table `calls`. RLock — reentrant (record/query
hold it and call _db() which re-acquires).
"""
import os, sqlite3, datetime, threading, hashlib, inspect, contextlib
from typing import Optional

from . import config

_conn = None
_lock = threading.RLock()
_local = threading.local()
_PKG = os.path.dirname(os.path.abspath(__file__))


# ── opt-in flags ──
def _truthy(v):
    return v in (True, "true", "1", 1)


def enabled():
    if os.getenv("SPENDGUARD_CALLS"):
        return os.getenv("SPENDGUARD_CALLS") not in ("0", "false", "")
    return _truthy(config._cfg_get("calls", "enabled", False))


def _store_prompts(): return _truthy(config._cfg_get("calls", "store_prompts", False))
def _snip():          return int(config._cfg_get("calls", "snippet_len", 200) or 200)


# ── intent / chain context (thread-local; safe under ThreadPool) ──
def current():
    return getattr(_local, "ctx", {})


def set_context(intent: Optional[str] = None, chain: Optional[str] = None) -> None:
    c = dict(current())
    if intent is not None:
        c["intent"] = intent
    if chain is not None:
        c["chain"] = chain
    _local.ctx = c


@contextlib.contextmanager
def context(intent: Optional[str] = None, chain: Optional[str] = None):
    """`with spendguard.context(intent='loinc-typing', chain='run-42'): ...` tags the calls inside.

    On exit it emits a per-FLOW spend receipt (what ran · tokens · est→actual · running tally) — see receipt.py.
    The receipt is verbosity-gated, goes to stderr, and is fully guarded: it NEVER raises into your code."""
    prev = getattr(_local, "ctx", None)
    set_context(intent=intent, chain=chain)
    start = (_max_rowid(), _flow_start_usd())          # flow window: (last call rowid, billed-$ so far)
    try:
        yield
    finally:
        ctx = current()
        _local.ctx = prev or {}
        try:
            from . import receipt
            receipt.emit_flow(ctx.get("intent"), ctx.get("chain"), start)
        except Exception:
            pass


# ── flow aggregation (powers the per-flow receipt; degrades gracefully when call-logging is off) ──
def _flow_start_usd() -> float:
    try:
        from . import budget
        return budget.spent_since("1970-01-01")            # all workload billed-$ to date (cheap; small table)
    except Exception:
        return 0.0


def _max_rowid() -> int:
    if not enabled():
        return 0
    try:
        with _lock:
            r = _db().execute("SELECT COALESCE(MAX(rowid),0) FROM calls").fetchone()
        return int(r[0] or 0)
    except Exception:
        return 0


def flow_agg(since_rowid: int = 0, chain: Optional[str] = None):
    """Aggregate the calls logged since `since_rowid` (a flow window) → {n, in_tok, out_tok, cost, caller}, or None
    when per-call logging is off/unavailable (the receipt then falls back to the always-on budget-$ delta)."""
    if not enabled():
        return None
    try:
        q = ("SELECT COUNT(*), COALESCE(SUM(in_tok),0), COALESCE(SUM(out_tok),0), "
             "COALESCE(SUM(cost),0.0), MAX(caller) FROM calls WHERE rowid > ?")
        a = [int(since_rowid or 0)]
        if chain:
            q += " AND chain = ?"
            a.append(chain)
        with _lock:
            row = _db().execute(q, a).fetchone()
        if not row or not row[0]:
            return None
        return {"n": int(row[0]), "in_tok": int(row[1] or 0), "out_tok": int(row[2] or 0),
                "cost": float(row[3] or 0.0), "caller": row[4]}
    except Exception:
        return None


def caller():
    try:
        for fr in inspect.stack()[2:]:
            fn = fr.filename
            if not fn.startswith(_PKG) and "site-packages" not in fn and fn not in ("<string>", "<stdin>"):
                return f"{os.path.basename(fn)}:{fr.function}:{fr.lineno}"
    except Exception:
        pass
    return None


# ── storage ──
def _db():
    global _conn
    if _conn is None:
        with _lock:
            if _conn is None:
                c = sqlite3.connect(config.db_path(), timeout=10, check_same_thread=False)
                c.execute("PRAGMA journal_mode=WAL")
                c.execute("""CREATE TABLE IF NOT EXISTS calls(
                    id TEXT PRIMARY KEY, ts TEXT, chain TEXT, intent TEXT, caller TEXT,
                    provider TEXT, model TEXT, kind TEXT,
                    in_tok INTEGER, out_tok INTEGER, cost REAL, latency REAL,
                    prompt_hash TEXT, prompt_snip TEXT, output_snip TEXT, finish TEXT,
                    quality TEXT, quality_src TEXT, quality_conf REAL)""")
                c.execute("CREATE INDEX IF NOT EXISTS idx_calls_chain ON calls(chain)")
                c.execute("CREATE INDEX IF NOT EXISTS idx_calls_intent ON calls(intent)")
                c.execute("CREATE INDEX IF NOT EXISTS idx_calls_ts ON calls(ts)")  # as_of/since range reads (calibrate, advise)
                if "quality_conf" not in [r[1] for r in c.execute("PRAGMA table_info(calls)").fetchall()]:
                    c.execute("ALTER TABLE calls ADD COLUMN quality_conf REAL")  # migrate older dbs
                c.commit()
                _conn = c
    return _conn


def _uuid():
    import uuid
    return uuid.uuid4().hex[:16]


def record(provider, model, kind, cost, in_tok=0, out_tok=0, latency=None,
           prompt=None, output=None, finish=None, intent=None, chain=None, who=None):
    """Record one call. Returns call_id (or None if logging is off). Never raises."""
    if not enabled():
        return None
    try:
        ctx = current()
        intent = intent or ctx.get("intent")
        chain = chain or ctx.get("chain")
        cid = _uuid()
        sp = _snip()
        ph = hashlib.sha256((prompt or "").encode("utf-8", "ignore")).hexdigest()[:16] if prompt else None
        psnip = prompt[:sp] if (prompt and _store_prompts()) else None
        osnip = output[:sp] if (output and _store_prompts()) else None
        ts = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
        with _lock:
            _db().execute(
                "INSERT INTO calls (id,ts,chain,intent,caller,provider,model,kind,in_tok,out_tok,"
                "cost,latency,prompt_hash,prompt_snip,output_snip,finish) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (cid, ts, chain, intent, who or caller(), provider, model, kind,
                 int(in_tok or 0), int(out_tok or 0), float(cost or 0), latency, ph, psnip, osnip, finish))
            _db().commit()
        # deferred implicit feedback: did THIS call reuse an earlier output in the same chain?
        if chain and prompt:
            _link_used(chain, prompt)
        return cid
    except Exception:
        return None


_CONF = {"explicit": 1.0, "judge": 0.95, "used": 0.6, "mined": 0.5}


def feedback(call_id: Optional[str], ok: bool = True, source: str = "explicit",
             confidence: Optional[float] = None) -> None:
    """Label a call's quality after the fact (judge verdict, human accept, downstream validation).
    Carries a confidence (explicit=1.0, judge=0.95, used=0.6, mined=0.5) the advisor weights by."""
    if not call_id:
        return
    conf = confidence if confidence is not None else _CONF.get(source, 0.7)
    try:
        with _lock:
            _db().execute("UPDATE calls SET quality=?, quality_src=?, quality_conf=? WHERE id=?",
                          ("good" if ok else "bad", source, conf, call_id))
            _db().commit()
    except Exception:
        pass


def insert(provider, model, kind, cost, in_tok=0, out_tok=0, ts=None, intent=None, chain=None,
           quality=None, quality_src=None, quality_conf=None, who="backfill"):
    """Low-level insert used by backfill (ungated). Returns call_id."""
    cid = _uuid()
    ts = ts or datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
    with _lock:
        _db().execute(
            "INSERT INTO calls (id,ts,chain,intent,caller,provider,model,kind,in_tok,out_tok,"
            "cost,quality,quality_src,quality_conf) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (cid, ts, chain, intent, who, provider, model, kind,
             int(in_tok or 0), int(out_tok or 0), float(cost or 0), quality, quality_src, quality_conf))
        _db().commit()
    return cid


def _link_used(chain, current_prompt):
    """Mark prior unlabeled calls in this chain 'used' if their output appears in this prompt."""
    if not _store_prompts():
        return
    try:
        with _lock:
            rows = _db().execute(
                "SELECT id, output_snip FROM calls WHERE chain=? AND quality IS NULL "
                "AND output_snip IS NOT NULL ORDER BY ts DESC LIMIT 10", (chain,)).fetchall()
            for cid, out in rows:
                if out and len(out) >= 12 and out[:80] in current_prompt:
                    _db().execute("UPDATE calls SET quality='good', quality_src='used', quality_conf=0.6 WHERE id=?", (cid,))
            _db().commit()
    except Exception:
        pass


def summary(intent=None):
    """Per (intent, model): calls, $ total, %good, and cost-per-good-result."""
    cond = ["(intent IS NULL OR intent NOT LIKE 'spendguard:%')"]   # exclude spendguard's own meta calls
    args = []
    if intent:
        cond.append("intent=?"); args.append(intent)
    # SQLi-safe (scanner false positive): `cond` holds only STATIC predicate strings ("intent=?"); every VALUE is bound
    # via `args` as a `?` parameter. The f-string below interpolates this fixed WHERE clause, never user data.
    where, args = ("WHERE " + " AND ".join(cond), tuple(args))
    with _lock:
        rows = _db().execute(
            f"""SELECT COALESCE(intent,'(none)'), COALESCE(model,'?'), COUNT(*), COALESCE(SUM(cost),0),
                   SUM(CASE WHEN quality='good' THEN 1 ELSE 0 END),
                   SUM(CASE WHEN quality='bad'  THEN 1 ELSE 0 END)
                FROM calls {where} GROUP BY intent, model ORDER BY SUM(cost) DESC""", args).fetchall()
    return rows


def tested_recently(intent, model=None, days=14, kinds=("realtime",)):
    """True iff there's a recent SMALL test for this intent — a realtime call (a batch-1 / PROMPT-CHECK on a
    handful of items) within `days`. The signal the batch-1 gate uses to tell "you tested this prompt shape before
    scaling it to a big batch" from "first thing you did for this intent was a huge batch." Model match is optional
    (a prompt/tool bug shows on any model); pass model to require the same one. Realtime-only by default because a
    prior *batch* row carries no request-count, so it can't prove a SMALL test was run."""
    if not enabled() or not intent:
        return False
    try:
        import datetime as _dt
        since = (_dt.datetime.now() - _dt.timedelta(days=int(days))).isoformat()
        # SQLi-safe (scanner false positive): `%s` is filled with the right COUNT of `?` placeholders, NOT values;
        # the kind values go through `args`. (Parameterized — no user data is concatenated into the SQL string.)
        q = "SELECT COUNT(*) FROM calls WHERE intent=? AND ts>=? AND kind IN (%s)" % ",".join("?" * len(kinds))
        args = [intent, since, *kinds]
        if model:
            q += " AND model=?"; args.append(model)
        with _lock:
            return _db().execute(q, args).fetchone()[0] > 0
    except Exception:
        return False


def cmd_summary(argv=None):
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--intent")
    a = ap.parse_args(argv)
    if not enabled():
        print("call logging is OFF — enable with `spendguard init` (calls.enabled) or SPENDGUARD_CALLS=1.")
        return 0
    rows = summary(a.intent)
    if not rows:
        print("no calls recorded yet.")
        return 0
    print(f"{'intent':<20}{'model':<22}{'calls':>7}{'$cost':>11}{'good%':>7}{'$/good':>10}")
    for intent, model, n, cost, good, bad in rows:
        labeled = (good or 0) + (bad or 0)
        goodpct = f"{100*good/labeled:.0f}%" if labeled else "—"
        per = f"${cost/good:.4f}" if good else "—"
        print(f"{intent[:19]:<20}{model[:21]:<22}{n:>7}{('$%.4f' % cost):>11}{goodpct:>7}{per:>10}")
    print("\ngood% = share of LABELED calls (feedback / judge / implicit 'used').  $/good = cost-per-good-result.")
    print("Label calls with spendguard.feedback(call_id, ok=...) or let chains infer 'used'.")
    return 0
