"""C3b — the dedicated BATCH-JOB tracker: the async, out-of-order, unbounded-timing lifecycle of Batch-API jobs, kept
SEPARATE from the realtime lane_queue (which is synchronous and deadline-bound). A batch is not a request — it is a
JOB (1 job : N task rows) that completes minutes-to-24h later, out of order, and can EXPIRE or FAIL as a whole unit.
That needs its own state machine, which is what this module is (Ash 2026-09-27: "a batch queue that has its own retry
and other logic like the base queue").

DIVISION OF LABOUR (no duplication):
  · lane_queue owns the TASK ROWS — mark_batched (→ queued_batch, holding the handle), collect_batched (settle rows BY
    custom_id as results arrive — the OUT-OF-ORDER, per-row half), batched_rows (the unsettled remainder),
    requeue_from_batch (idempotent fall-back of an exhausted batch's rows to realtime), settle.
  · this module owns the JOB — register (the durable record of the PAID handle, written by the SUBMITTER), and poll()
    (the lifecycle: settle what's ready, and on a batch that EXPIRED/FAILED, fall its unsettled rows back to realtime).

WHY poll() NEVER SUBMITS (the batch-level "retry" is a RE-DECISION, not a blind resubmit): submitting a NEW paid batch
inside poll would be a non-atomic paid write across two stores (submit → record) with no provider idempotency key — a
crash between them risks a DUPLICATE paid batch. So instead, an expired/failed batch's rows fall to the realtime retry
ladder (idempotent, crash-safe), and the drain planner re-offers them to a FRESH batch if still warranted — re-priced
and re-forecast, which is better than a blind resubmit. And that re-offer is itself exactly-once: submit_offload stamps
a deterministic offload key on every batch and reconciles against the provider's batch list before paying, so a
crash-retried offload ADOPTS its already-created batch instead of duplicating (see submit_offload). The row-level
realtime attempts (lane_queue max_attempts) bound the total, so nothing loops forever.

CRASH-SAFETY: a job's terminal fail moves rows (queue db) AND updates the job (batch_jobs db) — two resources, not one
transaction. Made crash-safe by a RESUMABLE 'failing' state: the job is marked 'failing' BEFORE its rows move, so a
crash mid-move is resumed next poll (idempotent requeue → 'failed'); the label always ends correct and no row is ever
double-processed. A deliberate spend/deadline stop always propagates.
"""
import threading
import time

from . import config

# OpenAI Batch completion window (grounded from the docs, 2026-09-27: processed within 24h). Used as the DEFAULT expiry
# when a caller gives none, so a batch that never resolves is failed-over instead of hanging forever. A named constant
# for a documented provider value; a caller passes expires_at to override per provider/job.
_DEFAULT_WINDOW_S = 24 * 3600
# The OpenAI batch STATUS enum (a FIXED API contract → parsing, not a meaning judgement) that means the batch will NOT
# deliver and must be failed over. 'completed' + the in-progress states (validating/in_progress/finalizing) are handled
# by readiness (batched_rows/collect), never here.
_TERMINAL_BAD = ("expired", "cancelled", "canceled", "failed")
# The batch-metadata field carrying submit_offload's DETERMINISTIC offload key (see submit_offload's exactly-once
# reconcile). Stamped on the batch at create so a crash-retried offload finds its ALREADY-created batch in the provider's
# list and ADOPTS it, instead of paying for a duplicate. Its value is a 32-char hash — within OpenAI's metadata limits.
_OFFLOAD_KEY_FIELD = "sg_offload_key"
# Statuses that make a keyed batch UN-adoptable on reconcile: the terminal-bad set PLUS 'cancelling' (on its way to
# cancelled). A keyed batch in one of these is a DEAD prior-cycle job whose rows already fell back to realtime, so a
# re-offer of the SAME rows must submit FRESH — never re-adopt a dead batch (which would strand its rows).
_DEAD_FOR_ADOPT = _TERMINAL_BAD + ("cancelling",)

_lock = threading.RLock()


def _ensure_batch_jobs_schema(c):
    c.execute("""CREATE TABLE IF NOT EXISTS batch_jobs(
        batch_id TEXT PRIMARY KEY, provider TEXT, model TEXT, intent TEXT, n_rows INTEGER,
        status TEXT, attempts INTEGER, submitted_ts REAL, updated_ts REAL, expires_at REAL, last_error TEXT)""")
    c.execute("CREATE INDEX IF NOT EXISTS idx_batch_jobs_status ON batch_jobs(status)")
    c.commit()


def _jobs_db():
    """The batch_jobs table on the shared pooled ledger connection (keyed 'batch_jobs') — reused, tuned, fork-safe;
    `_lock` serializes this module's read-modify-write sites (same discipline as calls._calls_db)."""
    return config.pooled_ledger_conn("batch_jobs", _ensure_batch_jobs_schema)


def register_batch(batch_id, provider, model, intent, n_rows, *, expires_at=None, attempts=1):
    """Record a submitted batch job (status 'open') — the DURABLE record of the PAID handle, written by the SUBMITTER
    right AFTER submit_chat_tasks returns a batch_id + lane_queue.mark_batched points the rows. REPLAY-SAFE: INSERT OR
    IGNORE, so re-registering an already-known batch_id is a NO-OP that preserves its lifecycle (never resets status/
    attempts). expires_at defaults to now + the provider window. Returns the batch_id, or None on a non-stop DB error
    (LOGGED with the id, never a silent loss; a stop/lock propagates)."""
    if not batch_id:
        return None
    now = time.time()
    exp = float(expires_at) if expires_at is not None else now + _DEFAULT_WINDOW_S
    try:
        with _lock:
            _jobs_db().execute(
                "INSERT OR IGNORE INTO batch_jobs (batch_id,provider,model,intent,n_rows,status,attempts,"
                "submitted_ts,updated_ts,expires_at,last_error) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (batch_id, provider, model, intent, int(n_rows or 0), "open", int(attempts), now, now, exp, None))
            _jobs_db().commit()
        return batch_id
    except Exception as e:
        from . import gate
        if gate.is_deliberate_stop(e):
            raise
        config.rollback_ledger_conn("batch_jobs")
        import sys
        print("[spendguard] batch_tracker.register: could NOT record batch %s (%s) — its handle is still in the "
              "submit result + the queue rows; collect_batched settles it row-level regardless, not lost"
              % (batch_id, type(e).__name__), file=sys.stderr, flush=True)
        return None


def _offload_key(row_ids, intent, model, provider):
    """A DETERMINISTIC key for THIS offload: hash(sorted row ids ⋮ intent ⋮ model ⋮ provider). The SAME row-set + intent
    + model + provider always yields the SAME key (so a crash-retry of the same offload finds its batch), and any
    different row-set / intent / model / provider yields a DIFFERENT key (so unrelated offloads never collide). Sorted,
    so row ORDER can't change the key. 32 hex chars — well within OpenAI's 512-char metadata-value limit. Pure
    derivation from fixed inputs (parsing, never a judgement)."""
    import hashlib
    ids = "\x1f".join(sorted(str(r) for r in (row_ids or [])))
    payload = "%s\x1e%s\x1e%s\x1e%s" % (ids, intent or "", model or "", provider or "")
    return hashlib.sha256(payload.encode("utf-8", "ignore")).hexdigest()[:32]


def _existing_offload_batch(offload_key, provider):
    """Exactly-once RECONCILE: is there already a LIVE batch carrying this offload_key (a prior attempt that submitted,
    perhaps crashing before it could mark/register)? Returns that batch_id to ADOPT, or None to submit fresh.

    The provider's OWN batch list — tagged at create with the deterministic key — is the durable record of 'did this
    offload already submit', so no local outbox is needed and the answer survives any crash. OpenAI-only: it is the only
    provider submit_chat_tasks can offload to (a non-OpenAI model is REFUSED there → no batch is created → nothing to
    reconcile → None is correct). A list failure PROPAGATES (submit_offload turns it into an offload error so the drain
    retries) — never swallowed into a blind submit, which is the duplicate hazard this removes."""
    if provider != "openai":
        return None
    from . import callio
    # No age boundary: lane_queue row ids are AUTOINCREMENT (never reused), so the offload key is globally unique to this
    # row-set — a batch carrying it is THIS offload's batch at any age, and adopting it (rather than capping by time and
    # re-submitting a completed-but-stale batch after a long outage) is what keeps it exactly-once. The scan is
    # recent-first (a crash-orphan is found early) and ceiling-bounded (raises rather than a truncated 'none').
    return callio.find_live_batch_by_metadata(_OFFLOAD_KEY_FIELD, offload_key, dead_statuses=_DEAD_FOR_ADOPT)


def submit_offload(intent, rows, batch_model, provider="openai", *, cap_dollars=None, expires_at=None):
    """Offload a set of task rows to the Batch API and TRACK the job — the ONE place the submit→record sequence lives
    (used by the drain autobatch path and any explicit offload op). EXACTLY-ONCE: reconcile → (adopt | submit) → mark →
    register. Returns {batch_id, marked, error[, adopted]}.

    THE EXACTLY-ONCE GUARANTEE (this used to be a caveat — why autobatch was default-off). submit is a PAID external
    call, and a crash in the window between submit returning a batch_id and the first mark committing used to leave a
    PAID batch whose rows were not yet marked; on lease reclaim those rows were re-offloaded → a DUPLICATE paid batch.
    Closed WITHOUT a local outbox and WITHOUT trusting a provider idempotency key, by using the provider's OWN batch
    list as the record: every batch is stamped at create with a DETERMINISTIC offload key (metadata sg_offload_key =
    _offload_key(rows, intent, model, provider)), and BEFORE paying we reconcile — is there already a LIVE batch
    carrying this key? If yes, ADOPT it (idempotent mark + register, no second submit); if no, submit fresh. So a
    crash-retry of the SAME rows finds its own batch (even if the crash lost the submit response entirely — the batch
    still exists on the provider with the key) and never double-pays. A TERMINAL-BAD keyed batch is a dead prior-cycle
    job (its rows already fell back), so it is IGNORED and a legitimate re-offer submits fresh. Concurrency is precluded
    upstream: the drain LEASES the rows, so two offloads of the same rows cannot run at once.

    mark comes BEFORE register so that even if register fails, the rows already carry the handle and collect_batched
    settles them row-level; and reconcile makes even that recoverable (the keyed batch is re-findable). A reconcile
    failure is NOT swallowed into a blind submit — it returns an offload error so the drain retries. A deliberate
    spend/deadline stop propagates."""
    from . import submit as _submit, lane_queue, gate
    rows = list(rows or [])
    if not rows:
        return {"batch_id": None, "marked": 0, "error": "no rows"}
    row_ids = [r["id"] for r in rows]
    key = _offload_key(row_ids, intent, batch_model, provider)

    # EXACTLY-ONCE step 1 — reconcile BEFORE paying: adopt an already-created batch for this exact row-set if one exists.
    try:
        existing = _existing_offload_batch(key, provider)
    except Exception as e:
        if gate.is_deliberate_stop(e):
            raise
        # a FAILED reconcile must NOT fall through to submit (that risks the duplicate) — name it and let the drain retry
        # this offload later (the leased rows stay put, work never lost). Loud, never a silent blind submit.
        return {"batch_id": None, "marked": 0, "error": "reconcile: %s" % (type(e).__name__)}
    if existing:
        marked = lane_queue.mark_batched(row_ids, existing, batch_model)   # idempotent (re-points queued_batch rows too)
        register_batch(existing, provider, batch_model, intent, marked, expires_at=expires_at)   # INSERT OR IGNORE
        return {"batch_id": existing, "marked": marked, "error": None, "adopted": True}

    # step 2 — first attempt: submit (estimate-first + $-capped) STAMPED with the offload key, then mark + register.
    tasks = [{"custom_id": r["id"], "content": r["task"], **({"system": r["system"]} if r.get("system") else {})}
             for r in rows]
    try:
        res = _submit.submit_chat_tasks(tasks, batch_model, intent=intent, cap_dollars=cap_dollars,
                                        metadata={_OFFLOAD_KEY_FIELD: key})   # the exactly-once tag on the created batch
    except Exception as e:
        if gate.is_deliberate_stop(e):
            raise                                      # a cap refusal / deadline HALTS — never a silent partial offload
        return {"batch_id": None, "marked": 0, "error": "submit: %s" % (str(e)[:80])}
    bid = res.get("batch_id") if isinstance(res, dict) else None
    if not bid:
        return {"batch_id": None, "marked": 0, "error": (res.get("error") if isinstance(res, dict) else "no batch_id")}
    marked = lane_queue.mark_batched(row_ids, bid, batch_model)   # per-row durable; custom_id = row id
    register_batch(bid, provider, batch_model, intent, marked, expires_at=expires_at)
    return {"batch_id": bid, "marked": marked, "error": None}


def _open_jobs(limit):
    try:
        with _lock:
            rows = _jobs_db().execute(
                "SELECT batch_id,intent,attempts,expires_at,status FROM batch_jobs "
                "WHERE status IN ('open','failing') ORDER BY submitted_ts LIMIT ?", (int(limit),)).fetchall()
        return [{"batch_id": r[0], "intent": r[1], "attempts": r[2], "expires_at": r[3], "status": r[4]}
                for r in rows]
    except Exception as e:
        from . import gate
        if gate.is_deliberate_stop(e):
            raise
        config.rollback_ledger_conn("batch_jobs")
        return []


def _set_status(batch_id, status, last_error=None):
    try:
        with _lock:
            _jobs_db().execute("UPDATE batch_jobs SET status=?, last_error=?, updated_ts=? WHERE batch_id=?",
                               (status, last_error, time.time(), batch_id))
            _jobs_db().commit()
    except Exception as e:
        from . import gate
        if gate.is_deliberate_stop(e):
            raise
        config.rollback_ledger_conn("batch_jobs")


def _fail_over(bid, rows, why, attempts):
    """Terminal fail-over of a job (expired/failed, or a 'failing' job resumed after a crash): fall its unsettled rows
    to the realtime retry ladder via the IDEMPOTENT, state-guarded requeue, then mark the job 'failed'. The drain
    planner may re-offer these rows to a fresh batch later (a re-decision). CRASH-SAFE: the caller marked the job
    'failing' (durable) BEFORE calling this, so a crash mid-move is resumed next poll — requeue_from_batch is a no-op on
    rows already moved, nothing is double-processed, and the label always ends 'failed'. Loud."""
    from . import lane_queue
    reason = "batch %s %s after %d attempt(s) — re-queued realtime (batch offload failed over)" % (bid, why, attempts)
    moved = lane_queue.requeue_from_batch([r["id"] for r in rows], reason) if rows else 0
    _set_status(bid, "failed", last_error=why)
    import sys
    print("[spendguard] batch_tracker: batch %s %s — %d row(s) re-queued to REALTIME (batch offload failed over, loud "
          "by design; the drain planner may re-batch them fresh)" % (bid, why, moved), file=sys.stderr, flush=True)
    return moved


def poll(limit=200, now=None):
    """One tracker tick (its OWN cadence — batches take minutes→24h — not every realtime drain round). poll() is the
    ACTIVE worker; status() is the read-only view. It NEVER submits a new batch (see the module docstring: submitting in
    poll would be a non-atomic paid write; the fail-over-to-realtime + drain re-offer path is the batch-level retry). It:
      0. RESUMES any 'failing' job (a fail that crashed mid-move) → finishes the idempotent requeue → 'failed'.
      1. ROW settlement (out-of-order, per custom_id): lane_queue.collect_batched() settles whatever is ready.
      2. per OPEN job, from its batch_status: all rows settled → 'settled'; TERMINAL-BAD/expired → 'failing' then
         _fail_over (rows → realtime, LOUD) → 'failed'; still running → left open; a row-read hiccup → left open + named.
    Returns {settled_rows, jobs_open, jobs_settled, jobs_failed, jobs_resumed, errors}. $0 (status + the already-billed
    collect). A deliberate spend/deadline stop propagates."""
    from . import lane_queue, callio, gate
    now = now or time.time()
    out = {"settled_rows": 0, "jobs_open": 0, "jobs_settled": 0, "jobs_failed": 0, "jobs_resumed": 0, "errors": []}
    try:
        col = lane_queue.collect_batched()
        out["settled_rows"] = int(col.get("done", 0)) + int(col.get("failed", 0))
    except Exception as e:
        if gate.is_deliberate_stop(e):
            raise
        out["errors"].append({"stage": "collect", "error": "%s: %s" % (type(e).__name__, str(e)[:60])})
    for job in _open_jobs(limit):
        bid = job["batch_id"]
        remaining = lane_queue.batched_rows(bid)
        if remaining is None:                          # row-read hiccup → leave OPEN, retry next tick (NOT 'settled')
            out["jobs_open"] += 1
            out["errors"].append({"batch_id": bid, "error": "batched_rows read failed — retry next tick"})
            continue
        if job["status"] == "failing":                 # RESUME a fail that crashed mid-move → finish it (idempotent)
            _fail_over(bid, remaining, job.get("last_error") or "resumed", job["attempts"])
            out["jobs_failed"] += 1
            out["jobs_resumed"] += 1
            continue
        if not remaining:                              # every row settled → the job is done
            _set_status(bid, "settled")
            out["jobs_settled"] += 1
            continue
        try:
            st = (callio.batch_status([bid]).get(bid) or {})
        except Exception as e:
            if gate.is_deliberate_stop(e):
                raise
            out["jobs_open"] += 1                      # status read failed → leave OPEN + named, retry next tick
            out["errors"].append({"batch_id": bid, "error": "status: %s" % (type(e).__name__)})
            continue
        bad = (st.get("status") in _TERMINAL_BAD) or (job.get("expires_at") and now > float(job["expires_at"]))
        if not bad:
            out["jobs_open"] += 1                      # validating/in_progress/finalizing → still running, leave open
            continue
        why = st.get("status") or ("expired@%.0f" % now)
        # EXPIRED/FAILED → CRASH-SAFE fail-over: mark 'failing' (durable intent) BEFORE moving rows, so a crash mid-move
        # is resumed at step 0 next tick (idempotent) and the label always ends 'failed' — never mislabelled 'settled'.
        _set_status(bid, "failing", last_error=why)
        _fail_over(bid, remaining, why, job["attempts"])
        out["jobs_failed"] += 1
    return out


def status():
    """{open, failing, settled, failed, total} counts of tracked batch jobs — observability for the CLI / receipt. $0.
    Returns {} on a non-stop DB error (a stop/lock propagates)."""
    try:
        with _lock:
            rows = _jobs_db().execute("SELECT status, COUNT(*) FROM batch_jobs GROUP BY status").fetchall()
        out = {"open": 0, "failing": 0, "settled": 0, "failed": 0}
        for st, n in rows:
            out[st] = int(n)
        out["total"] = sum(v for k, v in out.items() if k != "total")
        return out
    except Exception as e:
        from . import gate
        if gate.is_deliberate_stop(e):
            raise
        config.rollback_ledger_conn("batch_jobs")
        return {}
