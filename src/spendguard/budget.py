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
                if "key_fp" not in cols:                       # which provider key served the call (sha8:last4, local-only)
                    c.execute("ALTER TABLE charges ADD COLUMN key_fp TEXT DEFAULT ''")
                c.execute("CREATE INDEX IF NOT EXISTS idx_day ON charges(day)")
                c.execute("CREATE INDEX IF NOT EXISTS idx_charges_conv ON charges(conv_id)")  # chat↔charge joins (attribution)
                c.execute("CREATE INDEX IF NOT EXISTS idx_charges_keyfp ON charges(key_fp)")  # per-key spend view
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
    try:
        fp = config.key_fingerprint(provider)          # which key served this call (env-resolved proxy; ''=unknown)
    except Exception:
        fp = ""
    now = datetime.datetime.now(datetime.timezone.utc)
    with _lock:
        _db().execute("INSERT INTO charges (ts,day,provider,model,kind,cost,project,conv_id,key_fp) VALUES (?,?,?,?,?,?,?,?,?)",
                      (now.isoformat(timespec="seconds"), now.strftime("%Y-%m-%d"),
                       provider or "?", model or "?", kind, float(cost), proj or "", conv or "", fp))
        _db().commit()


def projects_for_conv(conv):
    """Distinct repos (projects) THIS conversation touched — its workload charges. Powers the contextual receipt's
    collapsed view (a conversation can span repos; this very chat touched llm-spendguard + manga2anime + lmm)."""
    if not conv:
        return []
    with _lock:
        rows = _db().execute("SELECT DISTINCT project FROM charges WHERE conv_id = ? AND project != '' "
                             "AND (kind IS NULL OR kind != 'meta')", (str(conv),)).fetchall()
    return sorted(r[0] for r in rows if r[0])


def all_projects():
    """All repos with workload charges (the expanded all-repos view)."""
    with _lock:
        rows = _db().execute("SELECT DISTINCT project FROM charges WHERE project != '' "
                             "AND (kind IS NULL OR kind != 'meta') AND (model IS NULL OR model <> ?)",
                             (_RECONCILED,)).fetchall()
    return sorted(r[0] for r in rows if r[0])


def ingest_remote(label, project, rows):
    """Roll a REMOTE box's realtime spend into the local ledger, IDEMPOTENTLY. Deletes any prior rows for this box
    (conv_id='remote:<label>') then inserts the current ones — so re-syncing a box REPLACES, never double-counts
    (a box's captioning runs on a real API key → actual-$ billed, attributed to its project). Returns (n, total)."""
    conv = "remote:" + str(label)
    proj = (project or "").strip().lower()
    n, total = 0, 0.0
    with _lock:
        db = _db()
        db.execute("DELETE FROM charges WHERE conv_id = ?", (conv,))
        for r in rows or []:
            cost = float(r.get("cost") or 0)
            if not cost:
                continue
            day = r.get("day") or datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")
            db.execute("INSERT INTO charges (ts,day,provider,model,kind,cost,project,conv_id) VALUES (?,?,?,?,?,?,?,?)",
                       (day + "T00:00:00+00:00", day, r.get("provider") or "?", r.get("model") or "?",
                        "remote", cost, proj, conv))
            n += 1; total += cost
        db.commit()
    return n, total


_RECONCILED = "(provider-batch)"   # marker model for reconciliation rows (the provider-truth gap), any project
# Realtime reconcile marker models (ledger_sync's _RT_MARKER/_RT_ORACLE_MARKER/_RT_RECON_MARKER — a drift guard in
# tests/test_true_down.py asserts the two stay identical). These rows MIRROR truth from another source (gate log /
# admin oracle / conversational reconstruction), so exclude_reconciled must drop them along with _RECONCILED — the
# trust check compares "what the gate recorded live" against those very sources, and counting a mirror row on the
# recorded side double-counts it (the $13.74 realtime drift bug).
_RT_MARKERS = ("(realtime-history)", "(realtime-oracle)", "(realtime-reconstructed)")
_MARKER_MODELS = (_RECONCILED,) + _RT_MARKERS
# True-down correction rows carry the REAL model (so by_dims NETS them against the estimate rows per
# (day,provider,model,kind,project) — the server ingest clamps negative cost, so a standalone negative marker-model
# row would be dropped there) and mark themselves via conv_id instead.
_TRUE_DOWN_CONV = "(true-down)"
# An estimate we KNOW is impossible (input > the model's context window — see gate._implausible_estimate).
# It is recorded, never deleted: a forensic record of what the estimator claimed. But it is quarantined out
# of every spend total, because a number that cannot describe a real request must not read as money spent.
QUARANTINE_CONV = "(impossible-estimate)"


def spent_since(day, project=None, conv=None):  # WORKLOAD spend only — excludes meta AND reconciled (historical) rows
    """Gate-recorded workload $ since `day`. Optionally SCOPE to a `project` (repo) and/or `conv` (conversation) —
    the receipt uses this to show what's relevant to the current repo/conversation, not a global sum."""
    cond = ["day >= ?", "(kind IS NULL OR kind != 'meta')", "(model IS NULL OR model <> ?)",
            "(conv_id IS NULL OR conv_id <> ?)"]     # quarantined: impossible estimates are never spend
    args = [day, _RECONCILED, QUARANTINE_CONV]
    if project is not None:
        cond.append("LOWER(project) = ?"); args.append(str(project).strip().lower())
    if conv is not None:
        cond.append("conv_id = ?"); args.append(str(conv))
    with _lock:
        r = _db().execute("SELECT COALESCE(SUM(cost),0) FROM charges WHERE " + " AND ".join(cond), args).fetchone()
    return float(r[0] or 0)


def suspect_batches(since):
    """Batch charges since `since`, joined to their `calls` row for the token counts, so an operator can SEE
    the arithmetic behind a number they doubt. Deliberately NOT automatic: recovering how many requests a
    past batch held is not always possible, and a repair that guesses the denominator would be the same class
    of mistake as the bug it is repairing. `spendguard quarantine --list` prints this; --ts acts on one row."""
    with _lock:
        # `calls` is created lazily (only when call logging is on), so its absence is NORMAL, not an error —
        # without it we still list the charges, just with no token counts to divide.
        has_calls = _db().execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='calls'").fetchone()
        # rowid is selected because it is the ONLY exact handle on a row: `ts` has second granularity and up
        # to six charges can share one second, so a ts-targeted repair could tag five innocent charges.
        sql = ("SELECT c.rowid, c.ts, c.day, c.provider, c.model, c.cost, COALESCE(c.project,''), "
               "       COALESCE(c.conv_id,''), k.in_tok, k.out_tok, COALESCE(k.caller,'') "
               "FROM charges c LEFT JOIN calls k ON k.ts = c.ts AND k.model = c.model AND k.kind = 'batch' "
               "WHERE c.kind='batch' AND c.day >= ? ORDER BY c.cost DESC") if has_calls else (
               "SELECT c.rowid, c.ts, c.day, c.provider, c.model, c.cost, COALESCE(c.project,''), "
               "       COALESCE(c.conv_id,''), NULL, NULL, '' "
               "FROM charges c WHERE c.kind='batch' AND c.day >= ? ORDER BY c.cost DESC")
        rows = _db().execute(sql, (since,)).fetchall()
    return [{"row": rid, "ts": t, "day": d, "provider": p_, "model": m, "cost": float(c or 0), "project": pr,
             "conv_id": cv, "in_tok": i, "out_tok": o, "caller": cl}
            for rid, t, d, p_, m, c, pr, cv, i, o, cl in rows]


def quarantine_charge(ts=None, reason="", row=None):
    """Tag ONE charge row as an impossible estimate. The row and its amount are untouched — only its conv_id
    marker changes, so it drops out of every total while staying fully auditable.

    Target by `row` (rowid — exact) or by `ts`. `charges.ts` has SECOND granularity and up to six charges can
    share one second, so a ts that matches more than one row RAISES rather than tagging all of them: silently
    excluding five innocent charges to quarantine one bad one would be a worse version of the bug this repairs.
    Returns the number of rows tagged (0 = nothing matched)."""
    with _lock:
        if row is not None:
            cur = _db().execute("UPDATE charges SET conv_id=? WHERE rowid=? AND conv_id <> ?",
                                (QUARANTINE_CONV, int(row), QUARANTINE_CONV))
        else:
            hits = _db().execute("SELECT rowid, model, cost FROM charges WHERE ts=? AND conv_id <> ?",
                                 (ts, QUARANTINE_CONV)).fetchall()
            if len(hits) > 1:
                raise ValueError(
                    "%d charges share the timestamp %s (%s) — refusing to quarantine all of them. Re-run with "
                    "--row <rowid> for the one you mean; `spendguard quarantine --list` shows the rowids."
                    % (len(hits), ts, ", ".join(f"row {h[0]}: {h[1]} ${h[2]:,.2f}" for h in hits)))
            cur = _db().execute("UPDATE charges SET conv_id=? WHERE ts=? AND conv_id <> ?",
                                (QUARANTINE_CONV, ts, QUARANTINE_CONV))
        n = cur.rowcount
        _db().commit()
    try:                                       # audit trail: WHAT changed, WHY, and what it was before
        _db().execute("INSERT INTO spend_audit (event_id, ts, actor, field, old_value, new_value, reason) "
                      "VALUES (?,?,?,?,?,?,?)",
                      (str(row if row is not None else ts),
                       datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
                       "quarantine_charge", "conv_id", "", QUARANTINE_CONV, reason))
        _db().commit()
    except Exception:
        pass                                   # audit shape varies by migration; never block the repair
    return n


def quarantined_since(day):
    """[{day, provider, model, cost, project}] of QUARANTINED rows since `day` — estimates the gate proved
    impossible. They are kept (forensics: what the estimator claimed, and when) and excluded from every total,
    so the honest thing is to SHOW them rather than let them vanish silently."""
    with _lock:
        rows = _db().execute(
            "SELECT day, COALESCE(provider,'?'), COALESCE(model,'?'), COALESCE(SUM(cost),0), COALESCE(project,''), "
            "COUNT(*) FROM charges WHERE day >= ? AND conv_id = ? GROUP BY day, provider, model, project "
            "ORDER BY SUM(cost) DESC", (day, QUARANTINE_CONV)).fetchall()
    return [{"day": d, "provider": p, "model": m, "cost": float(c or 0), "project": pr, "n": n}
            for d, p, m, c, pr, n in rows]


# ── reconciliation: make the LOCAL ledger reflect PROVIDER-billed truth (the gap = ungoverned/pre-ledger spend) ──
def by_provider_day(kind=None, since=None):
    """{(provider, day): $} of GATE-recorded spend (excludes reconciled rows) — the attributed side of reconcile."""
    cond = ["(model IS NULL OR model <> ?)", "(conv_id IS NULL OR conv_id <> ?)"]
    args = [_RECONCILED, QUARANTINE_CONV]           # like-for-like vs provider truth: quarantine excluded
    if kind:
        cond.append("kind=?"); args.append(kind)
    if since:
        cond.append("day >= ?"); args.append(since)
    where = "WHERE " + " AND ".join(cond)
    with _lock:
        rows = _db().execute(f"SELECT COALESCE(provider,'?'), day, COALESCE(SUM(cost),0) FROM charges {where} "
                             f"GROUP BY provider, day", args).fetchall()
    return {(p, d): float(c or 0) for p, d, c in rows}


def reconciled_by_project(since=None):
    """{project: $} of RECONCILED rows only (the provider-truth gap that reconcile_into_ledger attributed by
    conversation evidence). The 'attributed' side of the reconcile loop, complement to gate_by_project_day."""
    # A quarantined row carries a real model so it cannot match `model = _RECONCILED` — but the exclusion is
    # stated anyway rather than left as a fact someone has to re-derive. Cheap here, and the guard test
    # requires every cost aggregator to say it out loud.
    cond, args = ["model = ?", "(conv_id IS NULL OR conv_id <> ?)"], [_RECONCILED, QUARANTINE_CONV]
    if since:
        cond.append("day >= ?"); args.append(since)
    where = "WHERE " + " AND ".join(cond)
    with _lock:
        rows = _db().execute(f"SELECT COALESCE(NULLIF(project,''),'unattributed'), COALESCE(SUM(cost),0) "
                             f"FROM charges {where} GROUP BY 1", args).fetchall()
    return {p: float(c or 0) for p, c in rows}


def gate_by_project_day(kind=None, since=None):
    """{(project, day): $} of GATE-recorded (attributed) spend — excludes reconciled rows. Used to compute the
    per-project gap so the provider-truth gap is attributed by evidence, not dumped in one 'unattributed' bucket."""
    cond = ["(model IS NULL OR model <> ?)", "(conv_id IS NULL OR conv_id <> ?)"]
    args = [_RECONCILED, QUARANTINE_CONV]           # like-for-like vs provider truth: quarantine excluded
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


# ── estimate→actual true-down (ledger_sync.true_down writes these; the gate's batch rows are PRE-SUBMIT
#    estimates, so once the provider bills the actuals the ledger must come down to the billed truth) ──
def gate_batch_cells(since=None):
    """{(project, provider, model, day): $} of GATE-LIVE batch rows — the estimate base the true-down corrects.
    Excludes every reconcile marker model AND prior true-down rows (idempotence: corrections never feed the next
    correction). Full-dimension sibling of gate_by_project_day."""
    cond = ["kind='batch'", f"(model IS NULL OR model NOT IN ({','.join('?' * len(_MARKER_MODELS))}))",
            "(conv_id IS NULL OR conv_id NOT IN (?, ?))"]
    args = list(_MARKER_MODELS) + [_TRUE_DOWN_CONV, QUARANTINE_CONV]
    if since:
        cond.append("day >= ?"); args.append(since)
    with _lock:
        rows = _db().execute("SELECT COALESCE(NULLIF(project,''),'unattributed'), COALESCE(provider,'?'), "
                             "COALESCE(model,'?'), day, COALESCE(SUM(cost),0) FROM charges WHERE "
                             + " AND ".join(cond) + " GROUP BY 1, 2, 3, day", args).fetchall()
    return {(pr, p, m, d): float(c or 0) for pr, p, m, d, c in rows}


def record_true_down(day, provider, model, delta, project):
    """Insert ONE negative batch correction row: estimate − billed for this cell's share. Carries the REAL model
    (so by_dims nets it against the estimate rows before the SaaS push) and the true-down conv_id sentinel (so it
    is identifiable/clearable without a marker model). The original estimate rows are NEVER mutated — the ledger
    keeps both the estimate and the correction (forensic: what we thought + what it actually billed)."""
    with _lock:
        _db().execute("INSERT INTO charges (ts,day,provider,model,kind,cost,project,conv_id) VALUES (?,?,?,?,?,?,?,?)",
                      (day + "T00:00:00+00:00", day, provider or "?", model or "?", "batch",
                       -abs(float(delta)), project or "unattributed", _TRUE_DOWN_CONV))
        _db().commit()


def clear_true_down(since=None):
    """Remove prior true-down rows in the window so the correction is rebuilt idempotently each reconcile
    (billed actuals only grow as in-flight batches land, so each rebuild converges on the final billed $)."""
    with _lock:
        if since:
            _db().execute("DELETE FROM charges WHERE conv_id=? AND day >= ?", (_TRUE_DOWN_CONV, since))
        else:
            _db().execute("DELETE FROM charges WHERE conv_id=?", (_TRUE_DOWN_CONV,))
        _db().commit()


# ── spendguard's own advisor LLM use (segregated: own cap, own line, excluded from workload) ──
def record_meta(provider, model, cost):
    # spendguard's OWN spend → the llm-spendguard project, kept distinct by kind='meta' (NOT a separate project tag).
    record(provider, model, "meta", cost, project="llm-spendguard")


def meta_spent_since(day):
    with _lock:
        r = _db().execute("SELECT COALESCE(SUM(cost),0) FROM charges WHERE day >= ? AND kind='meta' "
                          "AND (conv_id IS NULL OR conv_id <> ?)",
                          (day, QUARANTINE_CONV)).fetchone()
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
    (provider-truth) rows / since a date. exclude_reconciled is essential for the LEAK check AND the trust check:
    reconciled rows MIRROR truth from another source (provider batch / gate log / reconstruction), so counting them
    as 'local gate-recorded' double-counts against that very source (coverage >100%, trust ratio inflated). It
    excludes ALL marker models; true-down correction rows are NOT markers (they correct the gate's own estimates)
    and stay included."""
    # Quarantined rows are excluded UNCONDITIONALLY, in every caller. They are estimates the gate proved
    # impossible; counting them as "accounted" is what let an invented $54.51 sit inside a leak check that
    # then reported no leak. A row that cannot describe a real request is not coverage of anything.
    cond, args = ["(conv_id IS NULL OR conv_id <> ?)"], [QUARANTINE_CONV]
    if kind:
        cond.append("kind=?"); args.append(kind)
    if exclude_meta:
        cond.append("(kind IS NULL OR kind != 'meta')")
    if exclude_reconciled:
        cond.append(f"(model IS NULL OR model NOT IN ({','.join('?' * len(_MARKER_MODELS))}))")
        args.extend(_MARKER_MODELS)
    if since:
        cond.append("day >= ?"); args.append(since)
    where = "WHERE " + " AND ".join(cond)
    with _lock:
        rows = _db().execute(f"SELECT day, COALESCE(SUM(cost),0) FROM charges {where} GROUP BY day", args).fetchall()
    return {d: float(v or 0) for d, v in rows}


def by_dims(since=None):
    """Per-day rows grouped by (day, provider, model, kind) for the SaaS roll-up push — the structured shape
    the server's /v1/ledger expects (vs by_day's flat {day: $}). Returns dicts with cost in $ and a call count."""
    # This is the SaaS PUSH payload. A quarantined row here would put an invented number on the org
    # dashboard, where nobody has the local context to question it.
    cond, args = ["(conv_id IS NULL OR conv_id <> ?)"], [QUARANTINE_CONV]
    if since:
        cond.append("day >= ?"); args.append(since)
    where = "WHERE " + " AND ".join(cond)
    with _lock:
        rows = _db().execute(
            f"SELECT day, COALESCE(provider,'?'), COALESCE(model,'?'), COALESCE(kind,'workload'), "
            f"COALESCE(project,''), COALESCE(SUM(cost),0), COUNT(*) FROM charges {where} "
            f"GROUP BY day, provider, model, kind, project", args
        ).fetchall()
    return [dict(day=d, provider=p, model=m, kind=k, project=pr, cost=float(c or 0), calls=int(n)) for d, p, m, k, pr, c, n in rows]


def by_key(since=None):
    """{(provider, key_fp): {'cost': $, 'calls': n}} of gate-recorded workload — per-KEY spend (which
    workspace/project key did this money flow through?). Excludes meta and every reconcile marker row
    (mirror/backfill rows carry no key). key_fp '' groups as '(none)' — rows recorded before key stamping
    existed, or where no key env was resolvable. LOCAL-ONLY view; fingerprints never leave the machine."""
    cond = ["(kind IS NULL OR kind != 'meta')",
            f"(model IS NULL OR model NOT IN ({','.join('?' * len(_MARKER_MODELS))}))",
            "(conv_id IS NULL OR conv_id NOT IN (?, ?))"]
    args = list(_MARKER_MODELS) + [_TRUE_DOWN_CONV, QUARANTINE_CONV]
    if since:
        cond.append("day >= ?"); args.append(since)
    with _lock:
        rows = _db().execute("SELECT COALESCE(provider,'?'), COALESCE(NULLIF(key_fp,''),'(none)'), "
                             "COALESCE(SUM(cost),0), COUNT(*) FROM charges WHERE "
                             + " AND ".join(cond) + " GROUP BY 1, 2", args).fetchall()
    return {(p, fp): {"cost": float(c or 0), "calls": int(n)} for p, fp, c, n in rows}


def ledger_start(kind=None):
    """Earliest day in the local ledger — spend before this wasn't recorded locally (pre-ledger). With `kind`,
    the earliest day for THAT axis: axes start recording at different times (realtime began long before batch),
    so a per-axis cutoff is what keeps the leak check from mislabeling one axis's pre-history as a leak."""
    with _lock:
        if kind:
            r = _db().execute("SELECT MIN(day) FROM charges WHERE kind=?", (kind,)).fetchone()
        else:
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
