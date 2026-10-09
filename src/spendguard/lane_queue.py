"""Durable LANE WORK QUEUE — accept work even when every lane is saturated, then DRAIN it onto idle plan capacity.

The problem this closes (Ash: "a queuing system so that even with high utilization we can address"). The dispatch
GOVERNOR (dispatch.py) is an in-process ADMISSION queue: overflow WAITS, and at the caller's deadline it raises
DispatchTimeout → the call FAILS or falls back to the metered API. Its author drew the line on purpose — "a broker
would be a different product" — because that layer is one job's I/O fan-out, ephemeral by design. A backlog that
must survive high utilization (and process restarts, and time) is a DIFFERENT need, and it is the one below.

This is a THIN durable layer ABOVE the governor, not a replacement for it (dispatch is left exactly as-is). It
mirrors the pattern the repo already trusts — saas.pull_commands/run_commands (the server enqueues, the client
leases + drains locally) — pointed at a LOCAL sqlite table instead:

  • enqueue()  NEVER blocks — that is the whole point. At 100% utilization you still accept the work; it is `pending`.
  • drain()    leases a batch and runs it through the EXISTING lane_balance.bulk_delegate (governor admission +
               bandit routing + $0 plan-served, API fallback only on a lane failure). So the queue adds durability,
               PRIORITY, and crash-recovery; bulk_delegate still does the concurrent, governed execution.
  • LEASE model — a leased row carries a lease_until; a worker that dies leaves its rows to be RECLAIMED (back to
    pending) once the lease expires, so a crash never loses work and never double-commits it. attempts is bounded
    by max_attempts; an exhausted task is marked `failed`, never retried forever.
  • PRIORITY — interactive work enqueues HIGH, bulk backfill LOW; each drain round serves the highest-priority
    pending intent first, so a 6k-item backfill never starves an interactive ask.
  • Two "high utilizations": lane saturation (the queue absorbs it, drains onto whichever plan frees first at $0)
    AND local CPU saturation (a load ceiling pauses leasing when the machine itself is thrashing).

PURE STATE persisted in the `lane_queue` table (the base sqlite, like lane_bandit) so nothing is re-paid or lost.
Every function is guarded to never raise into a caller. Knobs are named constants + advisor.* config — never a
literal at a call site.
"""
import datetime
import json
import os
import sqlite3
import time

from . import config

LEASE_S_DEFAULT = 300.0         # a leased task must settle within this window or it is reclaimed (worker presumed dead)
MAX_ATTEMPTS_DEFAULT = 10       # DURABLE retry budget: re-enqueue a RETRYABLE (transient) failure up to this many times
#   before marking it `failed`. This is the home of the "retry up to 10 to ENSURE success" guarantee (vendor_call.py
#   §RETRYABLE doctrine: in-process keeps a small 3 so a realtime caller isn't blocked, and the queue owns the 10 —
#   a queued call therefore gets up to (in-process 3) × (queue 10) tries, ASYNC, without blocking anyone). settle()
#   only spends this budget on TRANSIENT outcomes; a DETERMINISTIC failure fails FAST (see settle, class-aware retry).
# PARKING (Step 4 — backpressure): a task that could not get a governor slot (reason='dispatch_saturated') NEVER RAN,
# so it is not a failure — it is DEFERRED and retried when capacity frees, WITHOUT burning a failure-attempt. A park
# waits PARK_BACKOFF_S (so drain does not immediately re-saturate) and is bounded by MAX_PARKS (a no-SLA task can't
# park forever) AND by the row's own SLA deadline_ts (parking never pushes a call past the deadline it promised).
MAX_PARKS_DEFAULT = 50         # give up (→ failed) after this many capacity deferrals — the no-SLA safety ceiling
PARK_BACKOFF_S_DEFAULT = 10.0  # seconds a rate-blocked task waits before it can be re-leased (capacity-free backpressure)
IDLE_ROUNDS_DEFAULT = 2         # foreground drain stops after this many consecutive EMPTY leases (queue drained)
IDLE_SLEEP_DEFAULT = 2.0        # seconds to wait between empty leases / overload re-checks (foreground + daemon)
RETAIN_DAYS_DEFAULT = 7.0       # terminal rows (done/failed) older than this are archived to a log + removed from the
#                                 live queue, so it never accumulates forever (recent ones stay for --queue review)
PURGE_CHUNK_DEFAULT = 2000      # purge deletes terminal rows in bounded chunks — each a SHORT txn that releases the
#                                 write lock between chunks, so a backlog purge never holds the lock for minutes or
#                                 fetches the whole match-set into memory (the 2.7GB-under-lock scan, measured 2026-10-08)
PURGE_MIN_INTERVAL_S_DEFAULT = 3600.0   # purge is O(terminal rows), leasing is O(batch) — so DECOUPLE them: run purge at
#                                 most once/hour, not every drain cycle (every 300s was the 4-min-CPU cause)
ARCHIVE_MAX_MB_DEFAULT = 64.0   # lane_queue_archive.jsonl is append-only; rotate (keep one .1 generation) past this so it
#                                 cannot grow unbounded (it had reached 820MB, 2026-10-08)
_RESULT_CAP = 4000            # bytes of result JSON retained per row (audit/debug, not the whole payload)
_last_purge_ts = 0.0          # module-local throttle stamp for purge_due() — decouples purge from the drain cycle

# Priority convention (higher drains first): a delegated task someone is WAITING on jumps ahead of a big backfill,
# so a 6k-item bulk enqueue never starves interactive work sharing the same queue.
PRIORITY_BULK = 0               # backfill — the default for a large enqueue_many / `--enqueue`
PRIORITY_INTERACTIVE = 10       # a `delegate(enqueue=True)` task the caller wants back soon


def _qcfg(name, default):
    """A numeric advisor.* queue knob, defaulted — every queue parameter is CONFIG, never a hardcoded magic number.
    Delegates to the ONE advisor-knob reader (config.advisor_num), coerced to the default's declared type (int or
    float), so there is no second copy of the read / None / bad-value logic (an explicit 0 is honored; a bad value →
    the default)."""
    return config.advisor_num(name, default)


def _utcnow():
    return datetime.datetime.now(datetime.timezone.utc)


def _iso(dt):
    # UTC + timespec='seconds' → ISO strings sort lexicographically == chronologically, so lease-expiry can be a
    # plain string comparison (every timestamp this module writes uses THIS format, so the ordering is total).
    return dt.isoformat(timespec="seconds")


def _deadline_iso(sla_s):
    """An absolute SLA deadline (ISO ts) `sla_s` seconds from now, or None when no SLA is given — same ISO format as
    every other timestamp here, so the scheduler compares deadlines with a plain string ordering."""
    if sla_s is None:
        return None
    try:
        return _iso(_utcnow() + datetime.timedelta(seconds=float(sla_s)))
    except (TypeError, ValueError):
        return None


# The queue's connections are POOLED + tuned by the ONE shared implementation in config (pooled_ledger_conn /
# ledger_op / fresh_ledger_conn), keyed "lane_queue" — the per-op sqlite3.connect() dominated this write hot path
# (route_through_queue records every labelled call) and reuse is ~60x. These are thin, named delegations so the call
# sites + tests read `_queue_op` / `_queue_conn` / `_queue_db` locally while the pooling lives in ONE place.
_QUEUE_KEY = "lane_queue"


def _ensure_queue_schema(c):
    """Create the lane_queue table + its forward-only additive columns + the lease index (idempotent, cross-process
    safe). Run ONCE per pooled connection — at connection creation, and again whenever the pool reopens — so schema
    setup is tied to the connection's lifetime, not a module flag that could go stale against a replaced file."""
    c.execute("""CREATE TABLE IF NOT EXISTS lane_queue(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        intent TEXT NOT NULL, task TEXT NOT NULL, system TEXT, reasoning TEXT,
        priority INTEGER DEFAULT 0,
        state TEXT DEFAULT 'pending',        -- pending | leased | done | failed
        lease_until TEXT, attempts INTEGER DEFAULT 0, max_attempts INTEGER DEFAULT 3,
        worker TEXT, result TEXT, lane TEXT, billed INTEGER DEFAULT 0,
        created_ts TEXT, updated_ts TEXT)""")
    # FORWARD-ONLY additive migration for tables created before these columns existed: a queue item carries its
    # SERVICE CLASS ('realtime' | 'batch'), an absolute SLA DEADLINE, and PARKING state (defer_until = NOT leasable
    # until this ts, for capacity backpressure; parks = how many times deferred for saturation, bounded by MAX_PARKS,
    # distinct from failure `attempts`). SQLite has no ADD COLUMN IF NOT EXISTS; the repo pattern (bulkgate) is to
    # ATTEMPT the ALTER and swallow OperationalError. This is RACE-SAFE across the concurrent pooled connections — a
    # check-then-ALTER (PRAGMA table_info, then conditional ADD) is TOCTOU: under a concurrent fan two connections both
    # pass the check, the second ALTER raises "duplicate column name: sla_class", and that crashed the enqueue path
    # (observed in the 429 storm replay). Attempt-and-swallow cannot race: the column exists either way.
    for _col, _decl in (("sla_class", "TEXT DEFAULT 'batch'"), ("deadline_ts", "TEXT"),
                        ("defer_until", "TEXT"), ("parks", "INTEGER DEFAULT 0")):
        try:
            c.execute(f"ALTER TABLE lane_queue ADD COLUMN {_col} {_decl}")
        except sqlite3.OperationalError:
            pass                                  # already present (or a concurrent racer just added it) — goal holds
    # index the lease hot-path (pick highest-priority oldest pending) so a deep backlog stays cheap to poll.
    c.execute("CREATE INDEX IF NOT EXISTS lane_queue_pick ON lane_queue(state, priority DESC, id)")
    # index the PURGE path (terminal rows older than a cutoff). Without this, purge narrowed by state then SCANNED
    # every terminal row comparing updated_ts — on 137k terminal rows in a 2.7GB table that read most of the file
    # under the write lock every drain cycle (measured 2026-10-08). (state, updated_ts) makes it a bounded range scan.
    c.execute("CREATE INDEX IF NOT EXISTS lane_queue_purge ON lane_queue(state, updated_ts)")


def _queue_conn():
    """This thread's pooled, tuned, schema-ensured queue connection (reused; see config.pooled_ledger_conn). Opens the
    queue's OWN file (config.lane_queue_db_path()), NOT the spend.db money ledger — see lane_queue_db_path for why."""
    return config.pooled_ledger_conn(_QUEUE_KEY, _ensure_queue_schema, path=config.lane_queue_db_path())


def _reset_queue_conn():
    """Drop this thread's pooled queue connection (self-heal after an error); see config.reset_ledger_conn."""
    config.reset_ledger_conn(_QUEUE_KEY)


def _queue_op():
    """One queue op on the pooled connection — commit on success, rollback + drop the connection on error, re-raise
    (so each call site's own except still runs, e.g. _enqueue_leased's deliberate-stop propagation). See
    config.ledger_op. Use as `with _queue_op() as c:`."""
    return config.ledger_op(_QUEUE_KEY, _ensure_queue_schema, path=config.lane_queue_db_path())


def _queue_db():
    """A FRESH, closeable queue connection for EXTERNAL/one-off use (a test's manual setup, a CLI) — never the pooled
    connection, so closing it can't corrupt the pool. The hot internal path uses `_queue_op()` / `_queue_conn()`. See
    config.fresh_ledger_conn."""
    return config.fresh_ledger_conn(_ensure_queue_schema, path=config.lane_queue_db_path())


def migrate_from_ledger():
    """ONE-TIME move of the queue out of the spend.db money ledger into its own lane_queue.db (see
    config.lane_queue_db_path). Copies the NON-TERMINAL rows (pending/leased — the live work) and DROPs the old
    `lane_queue` table from the ledger, reclaiming its pages (every .backup snapshot + the B2 backup copy only used
    pages, so they shrink immediately; the live ledger file reuses the freed pages over time or on a later VACUUM).

    Terminal rows (done/failed) are NOT re-imported — they are the archival backlog purge() discards; migrating 137k
    of them would just recreate the bloat. `id` is NOT copied (the new db assigns fresh ids; a migrated leased row's
    stale lease simply expires and it is re-leased). Idempotent + re-runnable: once the ledger has no lane_queue table
    it is a no-op. Run with the drain DISABLED and AFTER the long-lived writers are on this code (so none recreates the
    table in the ledger via _ensure_queue_schema). Returns {moved, dropped, note} or {error}. Never raises."""
    import contextlib
    import sqlite3
    ledger = config.db_path()
    if ledger == config.lane_queue_db_path():
        return {"moved": 0, "dropped": False, "note": "queue db IS the ledger (no separate path configured) — nothing to move"}
    try:
        with contextlib.closing(sqlite3.connect(ledger, timeout=30)) as lc:
            lc.execute("PRAGMA busy_timeout=30000")
            if not lc.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='lane_queue'").fetchone():
                return {"moved": 0, "dropped": False, "note": "ledger has no lane_queue table (already migrated)"}
            src_cols = [r[1] for r in lc.execute("PRAGMA table_info(lane_queue)")]
            live = lc.execute("SELECT %s FROM lane_queue WHERE state NOT IN ('done','failed')"
                              % ",".join(src_cols)).fetchall()
        moved = 0
        if live:
            with _queue_op() as c:                               # ensures the new-db schema, then inserts
                dst_cols = {r[1] for r in c.execute("PRAGMA table_info(lane_queue)")}
                use = [col for col in src_cols if col in dst_cols and col != "id"]   # fresh ids in the new db
                idx = [src_cols.index(col) for col in use]
                c.executemany("INSERT INTO lane_queue (%s) VALUES (%s)" % (",".join(use), ",".join("?" * len(use))),
                              [[row[i] for i in idx] for row in live])
                moved = len(live)
        with contextlib.closing(sqlite3.connect(ledger, timeout=30)) as lc:
            lc.execute("PRAGMA busy_timeout=30000")
            lc.execute("DROP TABLE IF EXISTS lane_queue")        # reclaim ~2.7GB of pages → snapshots/backups shrink
            lc.commit()
        return {"moved": moved, "dropped": True,
                "note": "copied %d live row(s) to lane_queue.db; dropped lane_queue from the ledger" % moved}
    except Exception as e:
        return {"moved": 0, "dropped": False, "error": str(e)[:160]}


def optimize_queue_db():
    """Run `PRAGMA optimize` (refresh the query planner's stats) on the pooled connection. Called PERIODICALLY — at the
    end of a foreground drain — NOT per op: optimize can run ANALYZE, so on a per-op connection it would ADD cost, the
    opposite of the tuning goal. $0, best-effort, never raises."""
    try:
        _queue_conn().execute("PRAGMA optimize")
    except Exception:
        pass


def enqueue(intent, task, system=None, reasoning=None, priority=0, max_attempts=None,
            sla_class="batch", deadline_ts=None):
    """Append ONE task. NEVER blocks and never runs it — that is the point: work is accepted at any utilization and
    sits `pending` until a drainer has lane capacity. Returns the row id, or None on error."""
    return (enqueue_many(intent, [task], system=system, reasoning=reasoning, priority=priority,
                         max_attempts=max_attempts, sla_class=sla_class, deadline_ts=deadline_ts) or [None])[0]


def enqueue_many(intent, tasks, system=None, reasoning=None, priority=0, max_attempts=None,
                 sla_class="batch", deadline_ts=None):
    """Append many tasks of ONE intent in a single transaction (a 6k-item backfill is one commit). `sla_class`
    ('realtime' | 'batch') and `deadline_ts` (absolute ISO SLA deadline, or None) are stamped on every row so the
    scheduler/drain can serve tight-deadline realtime work first within capacity. Returns the new row ids in order;
    [] on error or empty input."""
    tasks = [t for t in (tasks or []) if t is not None]
    if not intent or not tasks:
        return []
    maxa = int(max_attempts if max_attempts is not None else _qcfg("queue_max_attempts", MAX_ATTEMPTS_DEFAULT))
    now = _iso(_utcnow())
    try:
        with _queue_op() as c:
            cur = c.cursor()
            ids = []
            for t in tasks:
                cur.execute("INSERT INTO lane_queue(intent,task,system,reasoning,priority,state,attempts,"
                            "max_attempts,sla_class,deadline_ts,created_ts,updated_ts) "
                            "VALUES(?,?,?,?,?, 'pending', 0, ?,?,?,?,?)",
                            (intent, t, system, reasoning, int(priority), maxa, sla_class, deadline_ts, now, now))
                ids.append(cur.lastrowid)
            c.commit()
            return ids
    except Exception:
        return []


def lease(n, worker=None, lease_s=None):
    """Atomically claim up to `n` pending tasks of the MOST URGENT pending intent — SLA/priority order: highest
    priority, then REALTIME before batch, then the tightest SLA deadline (deadline_ts asc, nulls last), then oldest —
    marking them `leased` with a fresh lease_until and attempts+1. First, within the SAME write lock, RECLAIMS
    expired leases (worker died): those under max_attempts return to `pending`, those at/over it become `failed`.
    Returns a list of {id,intent,task,system,reasoning,sla_class} dicts (empty if nothing is pending). Never raises.

    Intent-UNIFORM by design: a batch shares one intent so bulk_delegate routes it with that intent's bandit arms;
    a higher-priority row of a DIFFERENT intent is simply picked up on the next round."""
    n = max(1, int(n))
    lease_s = float(lease_s if lease_s is not None else _qcfg("queue_lease_s", LEASE_S_DEFAULT))
    worker = worker or f"drain-{os.getpid()}"
    now_dt = _utcnow()
    now, until = _iso(now_dt), _iso(now_dt + datetime.timedelta(seconds=lease_s))
    try:
        with _queue_op() as c:
            c.execute("BEGIN IMMEDIATE")                       # take the write lock BEFORE selecting → cross-process safe
            try:
                # reclaim expired leases: exhausted → failed, else → pending (retryable). attempts was already
                # incremented at lease time, so `attempts>=max_attempts` here means every allowed try is spent.
                c.execute("UPDATE lane_queue SET state='failed', updated_ts=? "
                          "WHERE state='leased' AND lease_until<? AND attempts>=max_attempts", (now, now))
                c.execute("UPDATE lane_queue SET state='pending', worker=NULL, updated_ts=? "
                          "WHERE state='leased' AND lease_until<? AND attempts<max_attempts", (now, now))
                # SLA/PRIORITY SCHEDULER (2b): pick the MOST URGENT pending intent — highest priority, then REALTIME
                # before batch, then the TIGHTEST deadline (deadline_ts ASC, nulls last), then oldest. So a realtime
                # item with a near SLA deadline preempts a batch backfill within the same capacity — 'what is needed
                # in the time that is available'. The order is TOTAL (id ASC breaks every tie), so leasing is stable.
                _ORDER = ("priority DESC, (sla_class='realtime') DESC, (deadline_ts IS NULL) ASC, deadline_ts ASC, "
                          "id ASC")
                # A PARKED row (defer_until in the future) is pending but NOT yet leasable — it is waiting out its
                # capacity backpressure; only rows whose defer window has passed (or that were never parked) are picked.
                _ready = "(defer_until IS NULL OR defer_until<=?)"
                top = c.execute("SELECT intent FROM lane_queue WHERE state='pending' AND " + _ready
                                + " ORDER BY " + _ORDER + " LIMIT 1", (now,)).fetchone()
                if not top:
                    c.execute("COMMIT")
                    return []
                intent = top[0]
                rows = c.execute("SELECT id,intent,task,system,reasoning,sla_class FROM lane_queue "
                                 "WHERE state='pending' AND intent=? AND " + _ready + " ORDER BY " + _ORDER
                                 + " LIMIT ?", (intent, now, n)).fetchall()
                for r in rows:
                    c.execute("UPDATE lane_queue SET state='leased', lease_until=?, attempts=attempts+1, "
                              "worker=?, updated_ts=? WHERE id=?", (until, worker, now, r[0]))
                c.execute("COMMIT")
                return [{"id": r[0], "intent": r[1], "task": r[2], "system": r[3], "reasoning": r[4],
                         "sla_class": r[5]} for r in rows]
            except Exception:
                c.execute("ROLLBACK")
                raise
    except Exception:
        return []


def settle(row_id, result):
    """Record the outcome of one leased task from a bulk_delegate result dict {text,lane,use_name,billed,error,reason}.
    Success (text and no error) → `done`. A task that could NOT get a governor slot (reason='dispatch_saturated') never
    RAN — it is not a failure but a capacity block, so it is PARKED (Step 4): deferred PARK_BACKOFF_S and retried when
    capacity frees, WITHOUT burning a failure-attempt (the lease's attempt is refunded), bounded by MAX_PARKS AND by the
    row's own SLA deadline_ts (a park never pushes a call past the deadline it promised). Any OTHER failure is settled
    CLASS-AWARE: a TRANSIENT outcome (vendor_call.RETRYABLE) retries (→ `pending`, counting an attempt) while attempts
    remain, else `failed`; a DETERMINISTIC outcome fails FAST (no wasted retries). A spend refusal / ledger LOCK
    propagates; any other hiccup is best-effort (the lease reclaims the row)."""
    result = result if isinstance(result, dict) else {}
    ok = bool(result.get("text")) and not result.get("error")
    # 'dispatch_saturated' is the STRUCTURED reason the runner emits when the governor had no slot within the deadline
    # (a fixed status code, not a meaning judgement) — the one outcome that is retryable-LATER rather than a failure.
    saturated = (not ok) and result.get("reason") == "dispatch_saturated"
    # 'queued_batch' is the STRUCTURED reason bulk_delegate(on_miss='batch') emits when it OFFLOADED this task to the
    # Batch API (the planner's predictive shed): the task neither failed nor finished — it is ASYNC in a batch, so it
    # must NOT retry realtime and must NOT count a failure-attempt. It becomes its own 'queued_batch' state holding the
    # batch handle, settled later by collect_batched. A fixed reason code + a present handle, never prose.
    batched = (not ok) and result.get("reason") == "queued_batch" and bool(result.get("batch"))
    now_dt = _utcnow()
    now = _iso(now_dt)
    try:
        with _queue_op() as c:
            row = c.execute("SELECT attempts, max_attempts, parks, deadline_ts FROM lane_queue WHERE id=?",
                            (row_id,)).fetchone()
            if not row:
                return
            attempts, maxa, parks, deadline_ts = row[0], row[1], (row[2] or 0), row[3]
            if ok:
                state, defer_until, new_attempts, new_parks = "done", None, attempts, parks
            elif saturated:
                backoff = float(_qcfg("queue_park_backoff_s", PARK_BACKOFF_S_DEFAULT))
                defer = _iso(now_dt + datetime.timedelta(seconds=backoff))
                over_sla = bool(deadline_ts) and defer >= deadline_ts     # a park would miss the SLA → honest deadline fail
                if parks >= int(_qcfg("queue_max_parks", MAX_PARKS_DEFAULT)) or over_sla:
                    state, defer_until, new_attempts, new_parks = "failed", None, attempts, parks
                else:                                                     # PARK: deferred, park counted, attempt REFUNDED
                    state, defer_until, new_attempts, new_parks = "pending", defer, max(0, attempts - 1), parks + 1
            elif batched:                                                 # OFFLOADED to the Batch API — await collection,
                state, defer_until, new_attempts, new_parks = "queued_batch", None, attempts, parks  # never realtime-retry
            else:
                # CLASS-AWARE durable retry: only a TRANSIENT outcome (vendor_call.RETRYABLE — transport_error /
                # overloaded) is worth re-enqueuing; re-running it can genuinely get a different answer, and this is
                # where the retry-to-10 reliability guarantee is spent. A DETERMINISTIC failure (payload_rejected,
                # refused, unfunded, deadline, truncated, empty, schema) fails IDENTICALLY on retry — the vendor_call
                # doctrine's "breaks on first occurrence regardless" — so it fails FAST, never burning the budget on a
                # hopeless re-run (Ash: "reduce retry, batch off"). The outcome is the vendor_call KIND the runner
                # stamped on the result; an ABSENT/unknown outcome is treated as retryable (conservative — matches the
                # pre-class behavior for a row a runner didn't classify, e.g. a governor/dispatch hiccup).
                from . import vendor_call as _vc
                oc = result.get("outcome")
                retryable = (oc is None) or (oc in _vc.RETRYABLE)
                state = ("pending" if attempts < maxa else "failed") if retryable else "failed"
                defer_until, new_attempts, new_parks = None, attempts, parks
            c.execute("UPDATE lane_queue SET state=?, result=?, lane=?, billed=?, lease_until=NULL, defer_until=?, "
                      "attempts=?, parks=?, updated_ts=? WHERE id=?",
                      (state, json.dumps(result)[:_RESULT_CAP], result.get("lane"),
                       1 if result.get("billed") else 0, defer_until, new_attempts, new_parks, now, row_id))
            c.commit()
            return True                                # the row WAS updated + committed — a caller may count it settled
    except Exception as _e:
        from . import provider_tokens as _pt          # the CANONICAL stop-or-locked predicate (same as _enqueue_leased)
        if _pt._stop_or_locked(_e):
            raise                                      # spend refusal / ledger LOCK PROPAGATES — never swallowed
        # else best-effort: a transient settle failure never crashes the drain loop (the lease reclaims the row) — but it
        # RETURNS FALSE so a caller (collect_batched) does NOT count an UN-settled row as done/failed/collected (F1).
        return False


def row_results(row_ids):
    """Read the current (state, parsed result, AND the originating request) for a set of rows by id — the durable read
    the storm batch executor polls after collect_batched settles. Returns {id: {"state": state, "result": {...},
    "task": <prompt str>, "system": <system str|None>}} for the rows FOUND (empty if none).

    WHY `task`/`system` are returned, not just the result: a reply can be meaningless WITHOUT the request that produced
    it — the classic case is an ORDINAL-into-a-candidate-list answer ("2" means candidates[2]), which is unparseable
    unless the exact candidate list (carried in the prompt) is still available at COLLECTION time. Collection happens
    hours later and may be a FRESH process whose in-memory custom_id→candidates map is gone, so the prompt must come
    back FROM THE DURABLE ROW. It is persisted at submit (lane_queue.task, written by _enqueue_leased), and surfacing it
    here lets the result and its parsing context travel together — the durable read is self-describing.

    A read failure PROPAGATES (not swallowed to {}) — a swallowed DB error would be indistinguishable from 'row not yet
    settled' and could become a FALSE poll-ceiling; the caller must see the failure and retry. Only a corrupt result
    JSON is tolerated (that one row's result is {}), since that is per-row, not a read failure."""
    ids = [int(r) for r in (row_ids or [])]
    if not ids:
        return {}
    out = {}
    with _queue_op() as c:
        rows = c.execute("SELECT id, state, result, task, system FROM lane_queue WHERE id IN (%s)"
                         % ",".join("?" * len(ids)), ids).fetchall()
    for rid, state, rjson, task, system in rows:
        try:
            res = json.loads(rjson) if rjson else {}
        except (ValueError, TypeError):
            res = {}
        out[rid] = {"state": state, "result": res, "task": task, "system": system}
    return out


def mark_batched(row_ids, batch_id, batch_model=None):
    """Move leased rows to the 'queued_batch' state, recording the batch handle (batch_id + model) so collect_batched
    can settle them later BY custom_id (== row id). Called by the drain OFFLOAD path right after submit_chat_tasks
    returns a batch_id. Stores the handle in the row's `result` (JSON), clears the lease, and does NOT count a
    failure-attempt (the work is not failing — it moved to the async path). Returns the count marked. Uses the same
    stop-or-locked propagation as settle (a ledger LOCK on a DB write must propagate, which is_deliberate_stop alone
    would not catch)."""
    ids = [r for r in (row_ids or []) if r]
    if not ids or not batch_id:
        return 0
    handle = json.dumps({"reason": "queued_batch", "batch": batch_id, "batch_model": batch_model})
    now = _iso(_utcnow())
    try:
        marked = 0
        with _queue_op() as c:
            for rid in ids:
                # state IN ('leased','queued_batch'): a leased row on FIRST submit, OR a queued_batch row being
                # RE-POINTED to a new batch_id when the tracker resubmits an expired/failed batch. COMMIT PER ROW so a
                # crash mid-loop leaves the already-marked rows DURABLE (never a whole-mark rollback); count the rows
                # ACTUALLY changed (a row in any other state is skipped) — never over-report by returning len(ids).
                cur = c.execute("UPDATE lane_queue SET state='queued_batch', result=?, lane='batch', "
                                "lease_until=NULL, updated_ts=? WHERE id=? AND state IN ('leased','queued_batch')",
                                (handle, now, rid))
                c.commit()
                marked += cur.rowcount if (cur.rowcount and cur.rowcount > 0) else 0
        return marked
    except Exception as _e:
        from . import provider_tokens as _pt
        if _pt._stop_or_locked(_e):
            raise
        import sys as _sysmb                           # NAMED trace (never a silent swallow): the affected rows + batch
        print("[spendguard] lane_queue.mark_batched: transient DB error (%s) — %d row(s) %s NOT marked queued_batch "
              "for batch %s; they stay LEASED and reclaim on lease expiry (work not lost, re-offloaded next round)"
              % (type(_e).__name__, len(ids), ids[:5], batch_id), file=_sysmb.stderr, flush=True)
        return 0


def collect_batched(limit=2000, model=None):
    """SETTLE side of the planner's batch offload: pull + settle rows in the 'queued_batch' state. Reads them, groups
    by their batch handle (batch_id, from result), pulls each via callio.collect_chat_tasks, and settles each row BY
    custom_id (== the row id). NOTHING is abandoned — every row is either settled or LEFT queued_batch to be retried on
    the NEXT collection round:
      · a result → settle done(text);
      · a per-request failure → settle (the normal retry/failed ladder);
      · a batch whose output is NOT ready → its rows stay queued_batch, re-collected next round (never blocks the 24h
        window);
      · a batch whose collect FAILS transiently → its rows stay queued_batch, re-collected next round (a NAMED entry in
        `collect_errors`, not a drop);
      · a row absent from BOTH results and failures (partial pull) → stays queued_batch, re-collected next round;
      · an ORPHANED row (its stored handle is missing/corrupt so it can NEVER be collected) → settled to the retry
        ladder so it re-runs realtime instead of sitting stuck forever, and named in `collect_errors`.
    $0 (a finished batch was already billed at submit). `model` defaults to config advisor.batch_model. A DELIBERATE
    spend/deadline stop from a collect propagates. Returns {rows, batches, done, failed, not_ready, collected,
    orphaned, collect_errors}."""
    from . import config
    model = model or config._cfg_get("advisor", "batch_model", None)
    try:
        with _queue_op() as c:
            rows = c.execute("SELECT id, intent, result FROM lane_queue WHERE state='queued_batch' LIMIT ?",
                             (int(limit),)).fetchall()
    except Exception as _e:
        from . import provider_tokens as _pt
        if _pt._stop_or_locked(_e):
            raise
        import sys as _syscb                           # NAMED trace: the read failed → collection skipped THIS round,
        print("[spendguard] lane_queue.collect_batched: transient DB error reading queued_batch rows (%s) — "
              "collection deferred to next round (queued_batch rows are durable, not lost)"
              % type(_e).__name__, file=_syscb.stderr, flush=True)
        return {"rows": 0, "batches": 0, "done": 0, "failed": 0, "not_ready": 0, "collected": 0, "orphaned": 0,
                "collect_errors": [{"error": "queue read failed: %s" % type(_e).__name__}]}
    groups, orphaned, group_model = {}, [], {}          # (batch_id, intent) -> [row_id]; orphaned = no usable handle
    for rid, intent, result_json in rows:
        try:
            _h = (json.loads(result_json) if result_json else {})
            handle, _bmodel = _h.get("batch"), _h.get("batch_model")
        except (ValueError, TypeError):
            handle, _bmodel = None, None
        if handle:
            groups.setdefault((handle, intent), []).append(rid)
            group_model[(handle, intent)] = _bmodel     # the batch's OWN model → its provider (picks the collect twin)
        else:
            orphaned.append(rid)                       # a queued_batch row with no usable batch handle — uncollectable
    from . import callio, gate as _gate, adapters
    out = {"rows": len(rows), "batches": len(groups), "done": 0, "failed": 0, "not_ready": 0, "collected": 0,
           "orphaned": len(orphaned), "not_settled": 0, "collect_errors": []}
    # UNSTICK orphaned rows: with no handle they can never be collected, so settle each to the retry ladder (re-runs
    # realtime) rather than leave it stuck in queued_batch forever — and name them (never a silent discard).
    _orphan_failed = []
    for rid in orphaned:
        if not settle(rid, {"error": "queued_batch row has no usable batch handle — re-queued for realtime retry"}):
            _orphan_failed.append(rid)                     # settle ITSELF failed → the orphan stays queued_batch — NAMED,
            #                                                not reported as recovered (F1: don't imply the orphan was handled)
    if orphaned:
        out["collect_errors"].append({"orphaned_no_handle": orphaned[:50], "count": len(orphaned)})
    if _orphan_failed:
        out["not_settled"] += len(_orphan_failed)
        out["collect_errors"].append({"orphan_settle_failed": _orphan_failed[:50], "count": len(_orphan_failed)})
    for (batch_id, intent), ids in groups.items():
        # The batch's OWN model (from its handle) picks the collect twin by provider — an Anthropic Message Batch settles
        # via collect_message_batch, an OpenAI chat batch via collect_chat_tasks; both return the same {results, failed,
        # not_ready, …} shape. Falls back to the function's `model` (and OpenAI) for an old row whose handle predates
        # batch_model being stored — unchanged behaviour for those.
        _bm = group_model.get((batch_id, intent)) or model
        try:
            _prov = adapters.provider_for(_bm) if _bm else "openai"
        except Exception:
            _prov = "openai"
        try:
            if _prov == "anthropic":
                res = callio.collect_message_batch(batch_id, intent, _bm)
            else:
                res = callio.collect_chat_tasks(batch_id, intent, _bm)
        except Exception as e:
            if _gate.is_deliberate_stop(e):
                raise                                  # a spend/deadline refusal HALTS collection, never continues past it
            # NAMED gap (never a silent skip — each batch is a unit of work): its rows stay queued_batch and are
            # RE-COLLECTED next round; the miss is surfaced by batch_id + row count so it is visible while it retries.
            out["collect_errors"].append({"batch_id": batch_id, "intent": intent, "rows": len(ids),
                                          "error": "%s: %s" % (type(e).__name__, str(e)[:60])})
            continue
        if batch_id in (res.get("not_ready") or []):
            out["not_ready"] += len(ids)
            continue                                   # output not ready → rows stay queued_batch, re-collected next round
        # KEY TYPE: a row id is an int, but custom_id round-trips through the Batch API as a STRING (submit coerces it),
        # so the collector returns STRING-keyed results. Match on str(rid) so an int row id finds its str-keyed result —
        # without this, real offloaded rows never settle (they sit queued_batch forever). Coerce both maps once.
        results = {str(k): v for k, v in (res.get("results") or {}).items()}
        failed = {str(k): v for k, v in (res.get("failed") or {}).items()}
        for rid in ids:
            _k = str(rid)
            if _k in results:
                # count done/collected ONLY when settle actually recorded it — a transient settle failure returns
                # falsy, and counting it would report the row collected while it stays queued_batch (F1). An un-settled
                # row is NAMED and stays queued_batch → re-collected next round (never lost, never over-reported).
                if settle(rid, {"text": results[_k], "lane": "batch"}):   # → done(text)
                    out["done"] += 1
                    out["collected"] += 1
                else:
                    out["collect_errors"].append({"batch_id": batch_id, "row": rid, "error": "settle failed — stays queued_batch"})
            elif _k in failed:
                if settle(rid, {"error": failed[_k]}):                    # → the normal retry/failed ladder
                    out["failed"] += 1
                    out["collected"] += 1
                else:
                    out["collect_errors"].append({"batch_id": batch_id, "row": rid, "error": "settle failed — stays queued_batch"})
            else:
                # id absent from BOTH results and failed (a partial pull — the batch returned some rows, not this one).
                # It stays queued_batch and is re-collected next round (never dropped) — but it is COUNTED (not_settled),
                # so a partially-returned batch never SILENTLY skips a row (each row is a unit of work).
                out["not_settled"] += 1
    return out


def batched_rows(batch_id):
    """The queued_batch rows STILL awaiting collection for `batch_id` — [{id, task, system, reasoning}] — so the batch
    tracker can RESUBMIT the unsettled remainder when a batch expires/fails (job-level retry). A row already settled
    (done/pending/failed) has LEFT queued_batch and is not returned. Returns None (NOT []) on a transient read failure,
    so the tracker treats it as 'unknown, retry next tick' and never mistakes a DB hiccup for 'all rows settled' (which
    would prematurely close the job). A stop/lock propagates. $0 read."""
    try:
        with _queue_op() as c:
            rows = c.execute("SELECT id, task, system, reasoning, result FROM lane_queue "
                             "WHERE state='queued_batch'").fetchall()
    except Exception as _e:
        from . import provider_tokens as _pt
        if _pt._stop_or_locked(_e):
            raise
        return None                                    # UNKNOWN (read failed) — never [] (which reads as 'all settled')
    out = []
    for rid, task, system, reasoning, result_json in rows:
        try:
            h = (json.loads(result_json) if result_json else {}).get("batch")
        except (ValueError, TypeError):
            h = None
        if h == batch_id:
            out.append({"id": rid, "task": task, "system": system, "reasoning": reasoning})
    return out


def requeue_from_batch(row_ids, reason):
    """Move queued_batch rows BACK to realtime 'pending' — the batch offload was EXHAUSTED/failed, so its rows fall to
    the realtime retry ladder. STATE-GUARDED + IDEMPOTENT (WHERE state='queued_batch'): a row already moved is left
    untouched, so a retried poll never re-processes it (the transition is safe to repeat — the batch_tracker relies on
    this for its non-atomic 'requeue rows then mark job failed' sequence). Commits PER ROW (durable partial progress).
    Returns the count ACTUALLY moved. A stop/lock propagates; any other transient error is logged with the ids (never a
    silent swallow) and returns the count moved so far (the rest stay queued_batch, retried next tick)."""
    ids = [r for r in (row_ids or []) if r]
    if not ids:
        return 0
    payload = json.dumps({"error": reason})
    now = _iso(_utcnow())
    moved = 0
    try:
        with _queue_op() as c:
            for rid in ids:
                cur = c.execute("UPDATE lane_queue SET state='pending', result=?, lane=NULL, worker=NULL, "
                                "lease_until=NULL, updated_ts=? WHERE id=? AND state='queued_batch'",
                                (payload, now, rid))
                c.commit()
                moved += cur.rowcount if (cur.rowcount and cur.rowcount > 0) else 0
        return moved
    except Exception as _e:
        from . import provider_tokens as _pt
        if _pt._stop_or_locked(_e):
            raise
        import sys as _sysrq                           # NAMED trace: the rows not yet moved stay queued_batch (retried)
        print("[spendguard] lane_queue.requeue_from_batch: transient DB error (%s) after moving %d/%d row(s) — the "
              "rest stay queued_batch and are retried next tick (not lost)"
              % (type(_e).__name__, moved, len(ids)), file=_sysrq.stderr, flush=True)
        return moved


def _enqueue_leased(intent, tasks, *, system=None, reasoning=None, priority=PRIORITY_INTERACTIVE,
                    sla_class="realtime", deadline_ts=None, lease_s=None):
    """Enqueue rows ALREADY OWNED by this worker (state='leased', attempts=1, a fresh lease_until) — the atomic
    enqueue the SYNC submit() fast-path needs so the drain daemon never races it for the just-added rows. Returns the
    ids in task order. A DELIBERATE stop (spend refusal / ledger LOCK) PROPAGATES — never swallowed to a [] that reads
    as 'nothing queued'; any OTHER hiccup returns [] LOUDLY (stderr) so the caller sees < len(tasks) rows and degrades
    honestly. If this worker dies before settle, the lease expires and the daemon reclaims the rows to 'pending'."""
    tasks = [t for t in (tasks or []) if t is not None]
    if not intent or not tasks:
        return []
    maxa = int(_qcfg("queue_max_attempts", MAX_ATTEMPTS_DEFAULT))
    lease_s = float(lease_s if lease_s is not None else _qcfg("queue_lease_s", LEASE_S_DEFAULT))
    worker = "submit-%d" % os.getpid()
    now_dt = _utcnow()
    now, until = _iso(now_dt), _iso(now_dt + datetime.timedelta(seconds=lease_s))
    try:
        with _queue_op() as c:
            cur = c.cursor()
            ids = []
            for t in tasks:
                cur.execute("INSERT INTO lane_queue(intent,task,system,reasoning,priority,state,attempts,"
                            "max_attempts,sla_class,deadline_ts,lease_until,worker,created_ts,updated_ts) "
                            "VALUES(?,?,?,?,?, 'leased', 1, ?,?,?,?,?,?,?)",
                            (intent, t, system, reasoning, int(priority), maxa, sla_class, deadline_ts,
                             until, worker, now, now))
                ids.append(cur.lastrowid)
            c.commit()
            return ids
    except Exception as _e:
        from . import provider_tokens as _pt          # reuse the CANONICAL stop-or-locked predicate (no duplicate name)
        if _pt._stop_or_locked(_e):
            raise                                      # spend refusal / ledger LOCK PROPAGATES — never a silent []
        import sys as _sys
        print("[spendguard] lane_queue._enqueue_leased: durable enqueue FAILED (%s: %s) — returning [] so the caller "
              "degrades HONESTLY (never a silent 'queued')." % (type(_e).__name__, str(_e)[:60]), file=_sys.stderr)
        return []


def submit(intent, tasks, *, priority=PRIORITY_INTERACTIVE, sla_class="realtime", sla_s=None, wait=True,
           system=None, reasoning=None, deadline_s=None, **bulk_kwargs):
    """THE FRONT DOOR — every request enters the durable queue here (priority + service class + SLA), so it is
    recorded, prioritisable and crash-recoverable. Two modes:
      • wait=True (the SYNC FAST-PATH, default for an interactive/realtime caller): enqueue the rows ALREADY LEASED to
        us (so the daemon never races them), run them INLINE through the now-bulletproof lane_balance.bulk_delegate
        (never crash, never empty), settle each, and RETURN {results: [...] in task order, ids, durable}. No
        drain-daemon loop latency — the row is durable (a dead worker's rows reclaim to pending), but the caller gets
        its answer NOW.
      • wait=False (async / batch backfill): enqueue 'pending' and RETURN {queued: ids, durable}; the lane-drain
        daemon runs them on spare capacity. A 6k-item backfill enqueues in one commit and never blocks.
    HONEST DEGRADATION: the queue is an ENHANCEMENT, never a GATE on the caller's result — if the durable store is
    (non-deliberately) unwritable the work STILL runs and its results are returned, but `durable` is False and a loud
    stderr line says so; submit NEVER claims a durability it did not get. A DELIBERATE stop (a spend refusal / ledger
    LOCK from the durable store, or a spend refusal from the run) PROPAGATES as a typed signal, never swallowed. The
    SLA (`sla_s` seconds) becomes an absolute deadline_ts on every row AND bounds the inline run's deadline. Extra
    bulk_delegate kwargs (schema, on_miss, base_fallback, lanes, …) pass through."""
    tasks = list(tasks)
    if not intent or not tasks:
        return {"queued": [], "results": ([] if wait else None), "durable": True}
    _dl = _deadline_iso(sla_s)
    if not wait:
        ids = enqueue_many(intent, tasks, system=system, reasoning=reasoning, priority=priority,
                           sla_class=sla_class, deadline_ts=_dl)
        return {"queued": ids, "results": None, "durable": len(ids) == len(tasks)}
    # SYNC FAST-PATH — own the rows from the start (no daemon race), run inline via the bulletproof engine, settle.
    ids = _enqueue_leased(intent, tasks, system=system, reasoning=reasoning, priority=priority,
                          sla_class=sla_class, deadline_ts=_dl)
    from . import lane_balance
    _run_dl = float(deadline_s if deadline_s is not None else (sla_s if sla_s else LEASE_S_DEFAULT))
    bulk_kwargs.pop("record_route", None)               # these rows ARE the queue — force record_route=False so each
    results = lane_balance.bulk_delegate(tasks, intent, system=system, reasoning=reasoning,  # per-task adapters.call
                                         deadline_s=_run_dl, record_route=False, **bulk_kwargs)  # doesn't open a 2nd row
    for rid, res in zip(ids, results):                 # settle every row we DID durably record
        settle(rid, res if isinstance(res, dict) else {"error": "no result"})
    _durable = len(ids) == len(tasks)
    if not _durable:
        # the durable enqueue was (non-deliberately) unavailable: the work ran and results are returned, but we do
        # NOT claim it was recorded/crash-recoverable — never a plausible-success that was never queued.
        import sys as _sys
        print("[spendguard] lane_queue.submit: durable enqueue UNAVAILABLE for intent %r — ran %d task(s) DIRECTLY "
              "and return results, but they were NOT durably recorded this run (durable=False)." % (intent, len(tasks)),
              file=_sys.stderr)
    return {"queued": ids, "results": results, "durable": _durable}


def record_open(intent, task, *, priority=PRIORITY_INTERACTIVE, sla_class="realtime", sla_s=None):
    """Open a durable record for ONE synchronous call about to run via its NORMAL path (the adapters
    route_through_queue front door): a leased row = observability + crash-recovery + priority/SLA metadata. Returns
    the row id, or None when the durable store is (non-deliberately) unavailable — the caller then runs UNRECORDED
    rather than blocked (the queue is an ENHANCEMENT, never a gate on the caller's result). A DELIBERATE stop (ledger
    lock / spend refusal) from the durable write PROPAGATES (never a silent None). Pair with record_close(rid, res)."""
    ids = _enqueue_leased(intent, [task], priority=priority, sla_class=sla_class, deadline_ts=_deadline_iso(sla_s))
    return ids[0] if ids else None


def record_close(rid, result):
    """Settle a record_open() row with the call's outcome (done / failed / retryable, via settle's own contract).
    No-op when rid is None (the open was unavailable). PROPAGATES a deliberate spend-refusal / ledger-LOCK (settle
    re-raises those, and the queue contract — see queue_depth — requires they halt), but SWALLOWS any other settle
    hiccup: a cleanup-time settle of an already-completed call must not crash the caller on a transient DB error."""
    if rid is None:
        return
    try:
        settle(rid, result if isinstance(result, dict) else {"error": "no result"})
    except Exception as e:
        from . import gate
        if gate.is_deliberate_stop(e):
            raise                                         # spend refusal / ledger lock → propagate (queue contract)
        import sys as _sys
        print(f"[spendguard] record_close: settle({rid}) hiccup swallowed ({type(e).__name__}: {str(e)[:60]}) — the "
              "row stays open for a later lease, never crashing this caller.", file=_sys.stderr)


def queue_depth():
    """{pending, leased, done, failed, parked} counts — the 'is anything queued' view (parallel to
    dispatch.queue_state). `parked` is the SUBSET of pending currently DEFERRED for capacity (defer_until in the
    future) — visible backpressure (Step 4), not a separate state. A spend refusal / ledger LOCK propagates; any
    other hiccup returns {} (a status read never crashes a caller)."""
    try:
        now = _iso(_utcnow())
        with _queue_op() as c:
            rows = c.execute("SELECT state, COUNT(*) FROM lane_queue GROUP BY state").fetchall()
            parked = c.execute("SELECT COUNT(*) FROM lane_queue WHERE state='pending' AND defer_until IS NOT NULL "
                               "AND defer_until>?", (now,)).fetchone()
        out = {"pending": 0, "leased": 0, "done": 0, "failed": 0}
        for st, n in rows:
            out[st] = n
        out["parked"] = int(parked[0]) if parked else 0
        return out
    except Exception as _e:
        from . import provider_tokens as _pt          # the CANONICAL stop-or-locked predicate (same as _enqueue_leased)
        if _pt._stop_or_locked(_e):
            raise                                      # spend refusal / ledger LOCK PROPAGATES — never masked as empty
        return {}


def pending_counts(realtime_only=True):
    """{intent: pending_count} — the per-INTENT durable backlog the queue planner offloads/paces (queue_planner.tick).
    queue_depth() answers 'how much is queued'; this answers 'of WHAT', keyed by intent (the queue's routing unit), so
    a per-vendor 429 forecast can reach intent-keyed rows. `realtime_only` skips rows already on the async batch path
    (sla_class='batch'), since those are what an offload would MOVE work onto, not from. A spend refusal / ledger LOCK
    propagates; any other hiccup returns {} (a status read never crashes a caller)."""
    try:
        with _queue_op() as c:
            if realtime_only:
                rows = c.execute("SELECT intent, COUNT(*) FROM lane_queue WHERE state='pending' AND "
                                 "COALESCE(sla_class,'realtime')!='batch' GROUP BY intent").fetchall()
            else:
                rows = c.execute("SELECT intent, COUNT(*) FROM lane_queue WHERE state='pending' "
                                 "GROUP BY intent").fetchall()
        return {r[0]: int(r[1]) for r in rows if r[0]}
    except Exception as _e:
        from . import provider_tokens as _pt
        if _pt._stop_or_locked(_e):
            raise
        return {}


def _bound_archive(path, max_mb=None):
    """Keep lane_queue_archive.jsonl bounded: when it exceeds max_mb, rotate it to `<path>.1` (replacing a prior .1)
    and start fresh — one generation retained, never unbounded growth. Best-effort; never raises."""
    try:
        cap = float(max_mb if max_mb is not None else _qcfg("queue_archive_max_mb", ARCHIVE_MAX_MB_DEFAULT))
        if cap <= 0 or not os.path.exists(path) or os.path.getsize(path) <= cap * 1024 * 1024:
            return
        os.replace(path, path + ".1")                      # atomic; keeps exactly one prior generation for review
    except OSError:
        pass


def purge_due(min_interval_s=None):
    """True if purge should run now — decouples the O(terminal-rows) purge from the O(batch) drain cycle. purge used to
    run EVERY drain (every 300s), scanning the whole terminal set under the ledger write lock; it only needs to run
    periodically. Tracks the last run in a module-local stamp (one daemon drives the daemon-mode drain). Config
    advisor.queue_purge_min_interval_s; default 1h."""
    import time as _t
    iv = float(min_interval_s if min_interval_s is not None else _qcfg("queue_purge_min_interval_s", PURGE_MIN_INTERVAL_S_DEFAULT))
    return (_t.time() - _last_purge_ts) >= max(0.0, iv)


def purge(retain_days=None, archive_path=None, chunk=None):
    """Bound the queue so it never accumulates forever: TERMINAL rows (done/failed) older than `retain_days` are
    APPENDED to an archive jsonl (a reviewable log) and then DELETED from the live table, in BOUNDED CHUNKS. Recent
    terminal rows stay in the queue for `--queue` review; pending/leased rows are NEVER touched. Returns {archived,
    deleted, archive} (or {error}). Never raises.

    Each chunk is a SHORT `BEGIN IMMEDIATE` txn (archive-before-delete preserved per chunk) that releases the write
    lock between chunks, so purging a large backlog never holds the ledger-grade lock for minutes nor fetches the
    whole match-set into memory — the (state, updated_ts) index makes the per-chunk SELECT a bounded range scan. At
    worst a crash between a chunk's archive-append and its delete-commit re-logs that chunk next run (a harmless dup in
    an append-only audit); it can never DELETE without having archived."""
    import time as _t
    global _last_purge_ts
    retain_days = float(retain_days if retain_days is not None else _qcfg("queue_retain_days", RETAIN_DAYS_DEFAULT))
    cutoff = _iso(_utcnow() - datetime.timedelta(days=retain_days))
    archive_path = archive_path or str(config.HOME / "lane_queue_archive.jsonl")
    chunk = max(1, int(chunk if chunk is not None else _qcfg("queue_purge_chunk", PURGE_CHUNK_DEFAULT)))
    cols = ("id", "intent", "task", "state", "attempts", "lane", "billed", "result", "created_ts", "updated_ts")
    archived = deleted = 0
    try:
        while True:
            with _queue_op() as c:
                c.execute("BEGIN IMMEDIATE")                   # lock before select→delete so a concurrent drainer can't race
                try:
                    rows = c.execute(f"SELECT {','.join(cols)} FROM lane_queue "
                                     "WHERE state IN ('done','failed') AND updated_ts < ? "
                                     "ORDER BY updated_ts LIMIT ?", (cutoff, chunk)).fetchall()
                    if not rows:
                        c.execute("COMMIT")
                        break
                    with open(archive_path, "a") as f:        # append-only audit log — archive BEFORE delete
                        for r in rows:
                            f.write(json.dumps(dict(zip(cols, r))) + "\n")
                    c.executemany("DELETE FROM lane_queue WHERE id=?", [(r[0],) for r in rows])
                    c.execute("COMMIT")
                except Exception:
                    c.execute("ROLLBACK")
                    raise
            archived += len(rows)
            deleted += len(rows)
            _bound_archive(archive_path)                       # keep the audit log bounded as we append
            if len(rows) < chunk:
                break                                          # last partial chunk → the backlog is cleared
        _last_purge_ts = _t.time()
        return {"archived": archived, "deleted": deleted, "archive": archive_path}
    except Exception as e:
        return {"archived": archived, "deleted": deleted, "error": str(e)[:120]}


def _overloaded(ceiling):
    """True if the local machine's 1-min load exceeds `ceiling` (>0). The 'local CPU saturation' guard — a drainer
    should not pile subprocess lanes onto a box that is already thrashing. False when ceiling is off (≤0) or load
    is unavailable (e.g. some platforms lack getloadavg)."""
    if not ceiling or ceiling <= 0:
        return False
    try:
        return os.getloadavg()[0] > float(ceiling)
    except (OSError, AttributeError):
        return False


def _acquire_drain_lock():
    """NON-BLOCKING single-instance guard so two drains never run at once. launchd single-instances per Label but
    RELAUNCHES an overrun run on exit (and a manual `--drain` can race the LaunchAgent), so overlap IS possible — two
    drains each spinning up the lane pool was part of the always-on CPU. Returns: an int fd (lock HELD — close to
    release), -1 (locking unavailable on this platform / infra error → proceed WITHOUT a guard, fail-open so a lock
    hiccup never blocks real draining), or None (another drain already holds it → caller returns immediately)."""
    try:
        import fcntl as _fcntl
        import os as _os
    except ImportError:
        return -1                                           # non-POSIX → no flock; degrade to unguarded, never block work
    try:
        fd = _os.open(str(config.HOME / "lane_queue.drain.lock"), _os.O_CREAT | _os.O_RDWR, 0o644)
    except OSError:
        return -1
    try:
        _fcntl.flock(fd, _fcntl.LOCK_EX | _fcntl.LOCK_NB)
    except OSError:
        try:
            _os.close(fd)
        except OSError:
            pass
        return None                                         # held by another drain → don't churn a second one
    return fd


def _release_drain_lock(fd):
    """Release the single-instance lock (closing the fd drops its flock). No-op for the -1/None sentinels."""
    if fd is None or fd < 0:
        return
    try:
        import os as _os
        _os.close(fd)
    except OSError:
        pass


def drain(worker=None, batch=None, lease_s=None, idle_rounds=None, idle_sleep=None,
          load_ceiling=None, forever=False, max_iters=None):
    """Lease → run via lane_balance.bulk_delegate → settle, repeatedly. FOREGROUND (forever=False): stop after
    `idle_rounds` consecutive empty leases (the queue is drained). DAEMON (forever=True): keep waiting for new work.
    Respects a local load ceiling (pauses leasing while the machine is overloaded). `max_iters` hard-caps total loop
    passes (a safety stop for bounded drains / tests / a persistently-overloaded box that would otherwise spin).
    Returns a summary {ran, done, failed, billed, by_lane, rounds} (rounds = passes that actually ran work). Never
    raises out.

    Reuses the whole existing execution path — governor admission, bandit routing, $0 plan-served, API fallback —
    so the queue is PURELY the durability + priority + crash-recovery layer on top."""
    from . import dispatch, lane_balance
    batch = int(batch or _qcfg("queue_batch", 0) or dispatch._limit("global_concurrency", 24))
    lease_s = float(lease_s if lease_s is not None else _qcfg("queue_lease_s", LEASE_S_DEFAULT))
    idle_rounds = int(idle_rounds if idle_rounds is not None else _qcfg("queue_idle_rounds", IDLE_ROUNDS_DEFAULT))
    idle_sleep = float(idle_sleep if idle_sleep is not None else _qcfg("queue_idle_sleep", IDLE_SLEEP_DEFAULT))
    ceiling = load_ceiling if load_ceiling is not None else _qcfg("queue_load_ceiling", 0.0)
    worker = worker or f"drain-{os.getpid()}"
    s = {"ran": 0, "done": 0, "failed": 0, "billed": 0, "by_lane": {}, "rounds": 0}
    _lock_fd = _acquire_drain_lock()
    if _lock_fd is None:                                     # another drain already running → don't start a second one
        s["skipped"] = "another drain is already running (single-instance lock held)"
        return s
    idle = iters = 0
    _last_poll = 0.0                                                     # throttle for the batch_tracker poll (below)
    while True:
        iters += 1
        if max_iters is not None and iters > int(max_iters):
            # NAMED, not a silent ceiling (F2): if work REMAINS when the cap ends the drain, say so — the durable rows
            # persist for the next drain / lease-recovery (never lost), but a caller must not read a capped stop as "all
            # done". Quiet when nothing remains (a bounded drain that finished cleanly).
            _qd = queue_depth()
            if _qd.get("pending", 0) or _qd.get("queued_batch", 0):
                import sys as _sysd
                print("[spendguard] drain: max_iters=%d ceiling reached with work REMAINING (%d pending, %d "
                      "queued_batch) — stopping this drain; the durable rows persist for the next drain."
                      % (int(max_iters), _qd.get("pending", 0), _qd.get("queued_batch", 0)), file=_sysd.stderr, flush=True)
            break                                                       # hard stop (bounded drains / tests / wedged pause)
        if _overloaded(ceiling):
            if idle_sleep > 0:
                time.sleep(idle_sleep)                                  # machine thrashing → hold off leasing, re-check
            continue
        rows = lease(batch, worker=worker, lease_s=lease_s)
        if not rows:
            idle += 1
            if not forever and idle >= idle_rounds:
                break                                                   # foreground: backlog drained, done
            if idle_sleep > 0:
                time.sleep(idle_sleep)
            continue
        idle = 0
        s["rounds"] += 1
        # PLANNER CONSULT (C, $0, NO execution): a cheap governor read each round so a PREDICTED 429 is visible DURING
        # the drain, pointing at `spendguard plan-queue` for the full offload plan. forecast() only reads live state; it
        # never spends and never mutates the queue. Config-gated (queue.planner_tick, default on); a hiccup never breaks
        # the drain. The ACTING half (auto-submit the offload) is the separate, config-gated, estimate-first C3 executor.
        if _qcfg("queue_planner_tick", 1):
            try:
                from . import queue_planner
                _fc = queue_planner.forecast()
                if _fc.get("at_risk"):
                    import sys as _sysqp
                    print("[spendguard] queue planner: %d vendor(s) at 429-risk (%s) — see `spendguard plan-queue`"
                          % (len(_fc["at_risk"]), ", ".join(_fc["at_risk"][:5])), file=_sysqp.stderr, flush=True)
            except Exception as _ce:
                from . import gate as _gce
                if _gce.is_deliberate_stop(_ce):
                    raise                                               # a refusal/deadline/containment HALTS, not pass
        # BATCH-TRACKER POLL (C3b) — settle ready batch offloads (out-of-order) + fail over expired ones. THROTTLED
        # (queue.planner_poll_s, default 30s) because batches take minutes→24h, so a per-round status poll would over-hit
        # the provider. $0 + NO submit (poll never spends); a deliberate stop propagates. Default on (queue.planner_poll).
        if _qcfg("queue_planner_poll", 1) and (time.time() - _last_poll) >= float(_qcfg("queue_planner_poll_s", 30)):
            _last_poll = time.time()
            try:
                from . import batch_tracker
                batch_tracker.poll()
            except Exception as _pe:
                from . import gate as _gpe
                if _gpe.is_deliberate_stop(_pe):
                    raise
        intent = rows[0]["intent"]
        # PREDICTIVE BATCH OFFLOAD (C3b, DEFAULT OFF: queue.planner_autobatch) — when this leased intent is saturated or
        # cheaper-as-batch AND batch-eligible, offload its rows to the Batch API instead of realtime (submit_offload is
        # estimate-first + $-capped; see its exactly-once caveat). The rows become queued_batch (tracked) and are settled
        # by the poll above. A submit that produced no batch is NAMED and falls through to realtime; any non-stop hiccup
        # also falls through — the leased work is NEVER lost. A deliberate spend/deadline stop propagates.
        if _qcfg("queue_planner_autobatch", 0):
            try:
                from . import queue_planner, batch_tracker
                _dec = queue_planner.should_offload(intent, len(rows))
                if _dec:
                    _off = batch_tracker.submit_offload(intent, rows, _dec["batch_model"],
                                                        provider=_dec.get("provider") or "openai",
                                                        cap_dollars=_qcfg("queue_batch_cap_usd", None))
                    if _off.get("batch_id") and _off.get("marked"):
                        s["batched"] = s.get("batched", 0) + _off["marked"]
                        continue                                        # rows now queued_batch (tracked) → skip realtime
                    if _off.get("error"):
                        import sys as _sysob                            # NAMED: offload skipped → running realtime
                        print("[spendguard] drain: batch offload of intent %r skipped (%s) — running realtime instead"
                              % (intent, _off["error"]), file=_sysob.stderr, flush=True)
            except Exception as _oe:
                from . import gate as _goe
                if _goe.is_deliberate_stop(_oe):
                    raise                                               # a spend refusal / deadline HALTS — never swallowed
                # any other hiccup → fall through to realtime (the leased work is never lost) — but NAMED + COUNTED, never
                # silent: a caller must be able to tell an offload that FAILED from one that never ran (F1). Mirrors the
                # _off.get("error") path above, which already names a submit that returned an error dict.
                s["offload_failed"] = s.get("offload_failed", 0) + 1
                import sys as _sysoe
                print("[spendguard] drain: batch offload of intent %r FAILED (%s: %s) — running realtime instead"
                      % (intent, type(_oe).__name__, str(_oe)[:80]), file=_sysoe.stderr, flush=True)
        # a leased batch is intent-uniform but may mix system/reasoning/sla_class — GROUP so bulk_delegate gets a
        # faithful (system, reasoning) per sub-batch AND a uniform sla_class, rather than silently applying the first
        # row's to all (no shortcut). sla_class flows to the governor so a 'batch' drain yields the realtime reserve.
        groups = {}
        for r in rows:
            groups.setdefault((r.get("system"), r.get("reasoning"), r.get("sla_class")), []).append(r)
        for (sys_, rea, sla_), grp in groups.items():
            results = lane_balance.bulk_delegate([g["task"] for g in grp], intent, system=sys_, reasoning=rea,
                                                 deadline_s=lease_s, sla_class=sla_, record_route=False)  # these ARE
            #                                       the queue rows — don't let each per-task adapters.call open a 2nd row
            for g, res in zip(grp, results):
                res = res if isinstance(res, dict) else {"error": "no result"}
                settle(g["id"], res)
                s["ran"] += 1
                if res.get("text") and not res.get("error"):
                    s["done"] += 1
                    ln = res.get("lane")
                    if ln:
                        s["by_lane"][ln] = s["by_lane"].get(ln, 0) + 1
                else:
                    s["failed"] += 1
                if res.get("billed"):
                    s["billed"] += 1
    optimize_queue_db()          # refresh planner stats ONCE at drain completion (periodic, never per-op) — now on the
    #                              small lane_queue.db, not the 4.4GB ledger, so ANALYZE is cheap
    _release_drain_lock(_lock_fd)
    return s
