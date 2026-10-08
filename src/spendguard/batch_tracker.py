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
    # offload_pending — the LOCAL half of exactly-once for a provider whose batch list carries NO metadata to reconcile
    # against (Anthropic Message Batches). OpenAI stamps the deterministic offload key into batch metadata and reconciles
    # purely from the provider's list (no local record needed — see _existing_offload_batch). Anthropic has no such slot,
    # so the submitter writes a row here keyed by the offload key BEFORE the paid create (batch_id NULL = 'submitting')
    # and fills batch_id in right after — so a crash-retry ADOPTS by local lookup, and the narrow create-then-crash
    # window is recovered by _recover_orphan_message_batch (provider scan, matched by the globally-unique row ids).
    c.execute("""CREATE TABLE IF NOT EXISTS offload_pending(
        offload_key TEXT PRIMARY KEY, provider TEXT, model TEXT, intent TEXT, n_rows INTEGER,
        batch_id TEXT, created_ts REAL, updated_ts REAL)""")
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


# A hard ceiling on the Anthropic orphan-recovery scan (the mirror of callio._RECONCILE_SCAN_CEILING): reaching it means
# the scan could NOT reach the created-floor window within this many batches, so it RAISES rather than concluding 'no
# orphan' — a truncated 'none' would read as 'nothing submitted, submit fresh' and DUPLICATE a paid batch.
_ANTHROPIC_RECONCILE_SCAN_CEILING = 20000

# The Anthropic Message Batch completion window (grounded from the docs: a batch is processed within 24h) + a margin.
# Its role in recovery: a batch THIS offload created (if the create reached the provider) will be ENDED — and so found
# by the orphan scan's custom_id match — within this window. So a 'submitting' record OLDER than this with NO ended
# match means the create never reached the provider (or its results expired >29d), and submitting fresh is safe. WITHIN
# the window a lost-id create may still be in flight, so the offload is HELD (never resubmitted) until it settles.
_ANTHROPIC_BATCH_WINDOW_S = 26 * 3600


class _OffloadInflightUnconfirmed(RuntimeError):
    """This offload's OWN prior attempt is unconfirmed (a 'submitting' pending record written just before the paid
    create, whose batch_id was lost to a crash) and recent enough that its batch may still be IN FLIGHT on the provider —
    where Anthropic exposes no custom_ids to confirm identity. HOLD — never resubmit past it (the double-pay this
    prevents): the drain retries, and once that batch ENDS the orphan scan matches it by custom_id and adopts it (or,
    past the completion window with still no match, proves it was never created and submits fresh). This is keyed to THIS
    offload's own pending record, never to an unrelated in-progress batch that merely shares a request count."""


def _pending_lookup(offload_key):
    """The local pending record for an Anthropic offload key, or None only when there is genuinely NO row. Anthropic
    batches carry no metadata to reconcile against, so this local row (written BEFORE the paid create) is the durable
    'did this offload already submit' record. FAILS CLOSED: a stop/lock propagates, and any OTHER read error RAISES too —
    an UNREADABLE record must never be mistaken for 'no prior submit', because a CONFIRMED record read as None would
    authorize a fresh paid batch and DUPLICATE the one it already holds. (submit_offload turns the raise into a reconcile
    error so the drain retries rather than blind-submitting.)"""
    from . import provider_tokens as _pt
    try:
        with _lock:
            row = _jobs_db().execute(
                "SELECT offload_key, provider, model, intent, n_rows, batch_id, created_ts FROM offload_pending "
                "WHERE offload_key=?", (offload_key,)).fetchone()
    except Exception as e:
        if _pt._stop_or_locked(e):
            raise
        config.rollback_ledger_conn("batch_jobs")
        raise                                          # fail CLOSED — an unreadable record is NOT 'no prior submit'
    if not row:
        return None
    return dict(offload_key=row[0], provider=row[1], model=row[2], intent=row[3], n_rows=row[4],
               batch_id=row[5], created_ts=row[6])


def _pending_note(offload_key, provider, model, intent, n_rows):
    """Durably note an offload is ABOUT to submit (batch_id NULL = 'submitting'), BEFORE the paid create. REPLACES any
    stale record for the key and stamps a FRESH created_ts (the floor the orphan scan bounds itself to). Returns
    created_ts. A stop/lock propagates; any other write error RAISES — without a durable pending record the submit
    cannot be made exactly-once, so the caller MUST refuse rather than blind-submit (fail closed, never a double-pay)."""
    from . import provider_tokens as _pt
    now = time.time()
    try:
        with _lock:
            _jobs_db().execute(
                "INSERT OR REPLACE INTO offload_pending (offload_key, provider, model, intent, n_rows, batch_id, "
                "created_ts, updated_ts) VALUES (?,?,?,?,?,?,?,?)",
                (offload_key, provider, model, intent, int(n_rows or 0), None, now, now))
            _jobs_db().commit()
        return now
    except Exception as e:
        if _pt._stop_or_locked(e):
            raise
        config.rollback_ledger_conn("batch_jobs")
        raise                                          # fail closed — no durable record ⇒ exactly-once not guaranteed


def _pending_confirm(offload_key, batch_id):
    """Record the batch_id the provider returned (pending → confirmed). After this a crash-retry adopts by local lookup,
    no provider scan. Best-effort: a stop/lock propagates; any other error is logged, NOT fatal — the batch_id is also on
    the queue rows (mark_batched) and re-findable by the orphan scan, so a lost confirm never loses the batch."""
    from . import provider_tokens as _pt
    try:
        with _lock:
            _jobs_db().execute("UPDATE offload_pending SET batch_id=?, updated_ts=? WHERE offload_key=?",
                               (batch_id, time.time(), offload_key))
            _jobs_db().commit()
    except Exception as e:
        if _pt._stop_or_locked(e):
            raise
        config.rollback_ledger_conn("batch_jobs")
        import sys
        print("[spendguard] batch_tracker._pending_confirm: could not confirm batch %s (%s) — recoverable via the "
              "queue rows + orphan scan" % (batch_id, type(e).__name__), file=sys.stderr, flush=True)


def _pending_clear(offload_key):
    """Remove a pending record (its batch is dead/expired, so a legitimate re-offer submits fresh). Best-effort."""
    from . import provider_tokens as _pt
    try:
        with _lock:
            _jobs_db().execute("DELETE FROM offload_pending WHERE offload_key=?", (offload_key,))
            _jobs_db().commit()
    except Exception as e:
        if _pt._stop_or_locked(e):
            raise
        config.rollback_ledger_conn("batch_jobs")


def _recover_orphan_message_batch(row_ids, created_floor, client=None):
    """Crash-after-accept recovery for an Anthropic offload: a 'submitting' pending record says a batch for THIS row-set
    was created, but its batch_id was lost to a crash between create() and confirm. Find that batch on the provider and
    ADOPT it, or prove it was never created and submit fresh — keyed to THIS offload's OWN record, NEVER to an unrelated
    batch that merely shares a request count. Returns a batch_id to ADOPT, or None to submit fresh; raises
    _OffloadInflightUnconfirmed to HOLD.

    Exactly-once rests on row ids being GLOBALLY UNIQUE (lane_queue AUTOINCREMENT, never reused): the offload submits each
    row with custom_id == its row id, so a batch carrying one of these custom_ids IS this offload's batch — a DEFINITIVE
    identity test, available only once the batch has ENDED (Anthropic exposes no custom_ids while it is in flight). So:
      · scan newest-first, bounded to batches created at/after `created_floor` (the pending row is written just before create);
      · an ENDED candidate (matching request count) whose first readable result custom_id ∈ row_ids → ADOPT its id;
      · an IN-PROGRESS / canceling candidate is SKIPPED, not held on — it cannot be confirmed, and it may be an UNRELATED
        job that merely shares a count (holding on it would false-block this offload — the bug this version removes);
      · after the window with no ended match: a pending record OLDER than the completion window means our batch (if any)
        would have ENDED and been found → it was never created (or expired >29d) → None (submit fresh); a RECENT record
        means our OWN lost-id create may still be in flight → raise _OffloadInflightUnconfirmed (HOLD until it settles,
        then this scan adopts it) — bounded by the window and tied to OUR record, never to an unrelated batch.
    Ceiling-bounded: a scan that cannot reach the window end RAISES (a truncated 'none' would DUPLICATE). $0 (a control-
    plane list + at most one finished-result line per candidate). A deliberate spend/deadline stop propagates."""
    import time
    from . import callio
    client = client or callio._anthropic_client()
    want = set(str(r) for r in (row_ids or []))
    if not want:
        return None
    seen = 0
    saw_unconfirmable = False       # did we see an IN-PROGRESS matching-count batch — a candidate we cannot confirm?
    for b in client.messages.batches.list(limit=100):      # anthropic SyncPage auto-paginates newest-first
        if seen >= _ANTHROPIC_RECONCILE_SCAN_CEILING:
            raise RuntimeError(
                "anthropic offload orphan scan passed %d batches without reaching the created-floor window — cannot "
                "confirm no orphan exists, so refusing to conclude 'none' (that would risk a DUPLICATE paid batch)."
                % _ANTHROPIC_RECONCILE_SCAN_CEILING)
        seen += 1
        created = getattr(b, "created_at", None)
        if created is not None and created_floor is not None and created.timestamp() < (created_floor - 5):
            break                                          # newest-first: past the floor (−5s skew) ⇒ window exhausted
        rc = getattr(b, "request_counts", None)
        total = (sum(int(getattr(rc, k, 0) or 0) for k in ("processing", "succeeded", "errored", "canceled", "expired"))
                 if rc is not None else None)
        if total is not None and total != len(want):
            continue                                       # a different-sized batch cannot be this exact row-set
        bid = getattr(b, "id", None)
        if getattr(b, "processing_status", None) == "ended" and getattr(b, "results_url", None):
            for res in client.messages.batches.results(bid):   # one readable custom_id is a DEFINITIVE identity test
                cid = getattr(res, "custom_id", None)
                if cid is None:
                    continue
                if str(cid) in want:
                    if not bid:
                        raise RuntimeError("anthropic orphan matched by custom_id but the batch carries no id — "
                                           "refusing to conclude 'none' (would risk a DUPLICATE paid batch).")
                    return bid                              # ADOPT — definitive custom_id match
                break                                      # first readable custom_id is not ours → a different batch
        else:
            saw_unconfirmable = True                        # an in-progress/canceling matching-count batch — note it, do NOT
            #                                                 hold on it directly (it may be unrelated); the decision is below
    # No ENDED batch of ours in the window. HOLD only when BOTH: an unconfirmable in-flight candidate exists AND our
    # pending record is recent enough that our own lost-id create could still be that in-flight batch. Otherwise submit
    # FRESH — an empty window (nothing in flight) proves the create never reached the provider, and a record older than
    # the completion window means our batch (if any) would have ENDED and been adopted above, so a still-in-flight batch
    # cannot be ours. This keys the hold to OUR record, never to an unrelated in-progress batch alone (bounded + rare).
    recent = created_floor is not None and (time.time() - created_floor) <= _ANTHROPIC_BATCH_WINDOW_S
    if saw_unconfirmable and recent:
        raise _OffloadInflightUnconfirmed(
            "a prior anthropic offload for this row-set is unconfirmed and a matching in-flight batch exists that "
            "cannot yet be identified (no custom_ids until it ends); holding until it settles (then adopted by "
            "custom_id) rather than risk a duplicate paid batch.")
    return None                                            # nothing in flight, or our window elapsed → never created → fresh


def _existing_offload_message_batch(offload_key, row_ids):
    """Anthropic twin of _existing_offload_batch: is there already a batch for this offload (a prior attempt that
    submitted, perhaps crashing before confirm)? Returns a batch_id to ADOPT, or None to submit fresh; raises
    _OffloadInflightUnconfirmed to HOLD. Anthropic has no batch metadata, so the LOCAL pending record is the record of
    'did this offload submit': a CONFIRMED record adopts directly (verifying the batch is still collectable — a dead or
    >29-day-expired one is cleared so the rows re-run), and an unconfirmed 'submitting' record is resolved against the
    provider by the orphan scan."""
    rec = _pending_lookup(offload_key)
    if not rec:
        return None                                        # no prior attempt → submit fresh
    from . import callio, gate
    client = callio._anthropic_client()
    if rec.get("batch_id"):
        try:
            b = client.messages.batches.retrieve(rec["batch_id"])
        except Exception as e:
            if gate.is_deliberate_stop(e):
                raise
            return rec["batch_id"]                         # can't verify now → adopt the known id (collect retries); never a fresh dup
        status = getattr(b, "processing_status", None)
        if status == "in_progress" or (status == "ended" and getattr(b, "results_url", None)):
            return rec["batch_id"]                          # live or still collectable → ADOPT
        _pending_clear(offload_key)                         # canceling, or ended-and-expired (>29d, no results) → dead → fresh
        return None
    return _recover_orphan_message_batch(row_ids, rec.get("created_ts"), client=client)   # 'submitting' → recover or prove-none


def submit_offload(intent, rows, batch_model, *, cap_dollars=None, expires_at=None,
                   shard_size=None, force=False):
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
    spend/deadline stop propagates.

    PROVIDER-AWARE (the exactly-once TAG differs by vendor). `provider` is derived from `batch_model` when the caller
    gives none — so an OpenAI batch_model keeps the OpenAI path (unchanged) and an Anthropic batch_model routes to the
    Messages Batch path. OpenAI stamps the key in BATCH METADATA and reconciles from the provider's list (no local
    record). Anthropic batches carry NO metadata, so the key lives in a LOCAL pending record written BEFORE the paid
    create, and the create-then-crash window is recovered by scanning the provider for a batch carrying these rows'
    (globally-unique) custom_ids — an in-flight one that can't yet be confirmed is HELD, never resubmitted."""
    from . import submit as _submit, lane_queue, gate, adapters, bulk_resilience
    rows = list(rows or [])
    if not rows:
        return {"batch_id": None, "marked": 0, "error": "no rows"}
    minimum = bulk_resilience._resilience_min_units()
    if shard_size is None and minimum > 1 and len(rows) >= minimum:
        shard_size = minimum - 1
    chunked = shard_size is not None and 0 < shard_size < len(rows)
    # This is the checkpointed path: every shard recurses through reconcile → create → durable row mark.
    # The provider sees request counts, not logical units packed upstream into each request.
    bulk_resilience.require_resilient(len(rows), chunked=chunked, checkpointed=True,
                                      force=force, where="batch_tracker.submit_offload")
    if chunked:
        shards = [rows[i:i + shard_size] for i in range(0, len(rows), shard_size)]
        per_cap = float(cap_dollars) / len(shards) if cap_dollars is not None else None
        results = [submit_offload(intent, shard, batch_model, cap_dollars=per_cap,
                                  expires_at=expires_at, force=force) for shard in shards]
        ids = [r["batch_id"] for r in results if r.get("batch_id")]
        return {"batch_ids": ids, "batch_id": None, "marked": sum(r.get("marked", 0) for r in results),
                "shards": len(shards), "errors": [r["error"] for r in results if r.get("error")],
                "error": "; ".join(r["error"] for r in results if r.get("error")) or None}
    # The offload RUNS on batch_model; its provider is DERIVED from the model — never taken from the caller — so the key
    # and the submit/collect path always match the ACTUAL vendor (a caller cannot force an OpenAI path onto an Anthropic
    # model). An OpenAI model derives 'openai' (the unchanged path); an Anthropic batch_model routes to the Messages Batch.
    try:
        provider = adapters.provider_for(batch_model)
    except Exception as e:
        return {"batch_id": None, "marked": 0, "error": "unknown provider for batch_model %r: %s" % (batch_model, type(e).__name__)}
    row_ids = [r["id"] for r in rows]
    key = _offload_key(row_ids, intent, batch_model, provider)

    # EXACTLY-ONCE step 1 — reconcile BEFORE paying: adopt an already-created batch for this exact row-set if one exists.
    try:
        if provider == "anthropic":
            existing = _existing_offload_message_batch(key, row_ids)
        else:
            existing = _existing_offload_batch(key, provider)
    except _OffloadInflightUnconfirmed as e:
        # a prior attempt is in-flight and not yet confirmable (Anthropic: no readable custom_ids mid-flight) — HOLD,
        # never resubmit past it. The drain retries; it adopts once the batch ends and its custom_ids become readable.
        return {"batch_id": None, "marked": 0, "error": "inflight-hold: %s" % (str(e)[:120])}
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

    # step 2 — first attempt: submit (estimate-first + $-capped), then mark + register. The exactly-once tag differs by
    # provider: OpenAI stamps the key in batch metadata; Anthropic (no metadata) writes a local pending record BEFORE the
    # paid create so a crash-retry adopts it / recovers the orphan.
    tasks = [{"custom_id": r["id"], "content": r["task"], **({"system": r["system"]} if r.get("system") else {})}
             for r in rows]
    try:
        if provider == "anthropic":
            try:
                _pending_note(key, provider, batch_model, intent, len(row_ids))   # durable BEFORE create — fail-closed
            except Exception as e:
                if gate.is_deliberate_stop(e):
                    raise
                return {"batch_id": None, "marked": 0, "error": "pending-note: %s" % (type(e).__name__)}
            res = _submit.submit_message_batch(tasks, batch_model, intent=intent, cap_dollars=cap_dollars,
                                               force=force)
        else:
            res = _submit.submit_chat_tasks(tasks, batch_model, intent=intent, cap_dollars=cap_dollars,
                                            metadata={_OFFLOAD_KEY_FIELD: key}, force=force)
    except Exception as e:
        if gate.is_deliberate_stop(e):
            raise                                      # a cap refusal / deadline HALTS — never a silent partial offload
        return {"batch_id": None, "marked": 0, "error": "submit: %s" % (str(e)[:80])}
    bid = res.get("batch_id") if isinstance(res, dict) else None
    if not bid:
        # No batch created (a cap refusal, build error, or lost response). The Anthropic pending record is LEFT in place
        # ('submitting'): the next reconcile scans the provider — if the create actually happened it ADOPTS, else it
        # proves none and submits fresh. This never double-pays; a persistently-refused offload just re-scans (bounded by
        # the drain's attempt limit), never a silent second batch.
        return {"batch_id": None, "marked": 0, "error": (res.get("error") if isinstance(res, dict) else "no batch_id")}
    if provider == "anthropic":
        _pending_confirm(key, bid)                     # pending → confirmed (a crash-retry now adopts by local lookup)
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
