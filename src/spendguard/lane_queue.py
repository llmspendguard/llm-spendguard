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
import time

from . import config

LEASE_S_DEFAULT = 300.0         # a leased task must settle within this window or it is reclaimed (worker presumed dead)
MAX_ATTEMPTS_DEFAULT = 3        # retry a task up to this many times before marking it `failed`
IDLE_ROUNDS_DEFAULT = 2         # foreground drain stops after this many consecutive EMPTY leases (queue drained)
IDLE_SLEEP_DEFAULT = 2.0        # seconds to wait between empty leases / overload re-checks (foreground + daemon)
RETAIN_DAYS_DEFAULT = 7.0       # terminal rows (done/failed) older than this are archived to a log + removed from the
#                                 live queue, so it never accumulates forever (recent ones stay for --queue review)
_RESULT_CAP = 4000            # bytes of result JSON retained per row (audit/debug, not the whole payload)

# Priority convention (higher drains first): a delegated task someone is WAITING on jumps ahead of a big backfill,
# so a 6k-item bulk enqueue never starves interactive work sharing the same queue.
PRIORITY_BULK = 0               # backfill — the default for a large enqueue_many / `--enqueue`
PRIORITY_INTERACTIVE = 10       # a `delegate(enqueue=True)` task the caller wants back soon


def _qcfg(name, default):
    """A numeric advisor.* knob, defaulted — every queue parameter is CONFIG, never a hardcoded magic number."""
    try:
        v = config._cfg_get("advisor", name, None)
        return type(default)(v) if v is not None else default
    except (TypeError, ValueError):
        return default


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
    # FORWARD-ONLY additive migration for tables created before sla_class/deadline_ts existed: a queue item carries its
    # SERVICE CLASS ('realtime' | 'batch') and an absolute SLA DEADLINE. SQLite has no ADD COLUMN IF NOT EXISTS, so the
    # live columns are checked first (idempotent; runs once per new column).
    _cols = {r[1] for r in c.execute("PRAGMA table_info(lane_queue)").fetchall()}
    if "sla_class" not in _cols:
        c.execute("ALTER TABLE lane_queue ADD COLUMN sla_class TEXT DEFAULT 'batch'")
    if "deadline_ts" not in _cols:
        c.execute("ALTER TABLE lane_queue ADD COLUMN deadline_ts TEXT")
    # index the lease hot-path (pick highest-priority oldest pending) so a deep backlog stays cheap to poll.
    c.execute("CREATE INDEX IF NOT EXISTS lane_queue_pick ON lane_queue(state, priority DESC, id)")


def _queue_conn():
    """This thread's pooled, tuned, schema-ensured queue connection (reused; see config.pooled_ledger_conn)."""
    return config.pooled_ledger_conn(_QUEUE_KEY, _ensure_queue_schema)


def _reset_queue_conn():
    """Drop this thread's pooled queue connection (self-heal after an error); see config.reset_ledger_conn."""
    config.reset_ledger_conn(_QUEUE_KEY)


def _queue_op():
    """One queue op on the pooled connection — commit on success, rollback + drop the connection on error, re-raise
    (so each call site's own except still runs, e.g. _enqueue_leased's deliberate-stop propagation). See
    config.ledger_op. Use as `with _queue_op() as c:`."""
    return config.ledger_op(_QUEUE_KEY, _ensure_queue_schema)


def _queue_db():
    """A FRESH, closeable queue connection for EXTERNAL/one-off use (a test's manual setup, a CLI) — never the pooled
    connection, so closing it can't corrupt the pool. The hot internal path uses `_queue_op()` / `_queue_conn()`. See
    config.fresh_ledger_conn."""
    return config.fresh_ledger_conn(_ensure_queue_schema)


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
                top = c.execute("SELECT intent FROM lane_queue WHERE state='pending' ORDER BY " + _ORDER
                                + " LIMIT 1").fetchone()
                if not top:
                    c.execute("COMMIT")
                    return []
                intent = top[0]
                rows = c.execute("SELECT id,intent,task,system,reasoning,sla_class FROM lane_queue "
                                 "WHERE state='pending' AND intent=? ORDER BY " + _ORDER + " LIMIT ?",
                                 (intent, n)).fetchall()
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
    """Record the outcome of one leased task from a bulk_delegate result dict {text,lane,use_name,billed,error}.
    Success (text and no error) → `done`. A failure retries (→ `pending`) while attempts remain, else `failed`.
    Never raises."""
    result = result if isinstance(result, dict) else {}
    ok = bool(result.get("text")) and not result.get("error")
    try:
        with _queue_op() as c:
            row = c.execute("SELECT attempts, max_attempts FROM lane_queue WHERE id=?", (row_id,)).fetchone()
            if not row:
                return
            attempts, maxa = row
            state = "done" if ok else ("pending" if attempts < maxa else "failed")
            c.execute("UPDATE lane_queue SET state=?, result=?, lane=?, billed=?, lease_until=NULL, updated_ts=? "
                      "WHERE id=?", (state, json.dumps(result)[:_RESULT_CAP], result.get("lane"),
                                     1 if result.get("billed") else 0, _iso(_utcnow()), row_id))
            c.commit()
    except Exception:
        pass


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
    results = lane_balance.bulk_delegate(tasks, intent, system=system, reasoning=reasoning,
                                         deadline_s=_run_dl, **bulk_kwargs)
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
    No-op when rid is None (the open was unavailable). Never raises."""
    if rid is not None:
        settle(rid, result if isinstance(result, dict) else {"error": "no result"})


def queue_depth():
    """{pending, leased, done, failed} counts — the 'is anything queued' view (parallel to dispatch.queue_state).
    Empty dict on error."""
    try:
        with _queue_op() as c:
            rows = c.execute("SELECT state, COUNT(*) FROM lane_queue GROUP BY state").fetchall()
        out = {"pending": 0, "leased": 0, "done": 0, "failed": 0}
        for st, n in rows:
            out[st] = n
        return out
    except Exception:
        return {}


def purge(retain_days=None, archive_path=None):
    """Bound the queue so it never accumulates forever: TERMINAL rows (done/failed) older than `retain_days` are
    APPENDED to an archive jsonl (a reviewable log) and then DELETED from the live table. Recent terminal rows stay
    in the queue for `--queue` review; pending/leased rows are NEVER touched. Returns {archived, deleted, archive}
    (or {error}). Never raises. The archive-append happens before the delete commits, so at worst a crash re-logs a
    row on the next run (harmless dup in an append-only audit) — it can never DELETE without having archived."""
    retain_days = float(retain_days if retain_days is not None else _qcfg("queue_retain_days", RETAIN_DAYS_DEFAULT))
    cutoff = _iso(_utcnow() - datetime.timedelta(days=retain_days))
    archive_path = archive_path or str(config.HOME / "lane_queue_archive.jsonl")
    cols = ("id", "intent", "task", "state", "attempts", "lane", "billed", "result", "created_ts", "updated_ts")
    try:
        with _queue_op() as c:
            c.execute("BEGIN IMMEDIATE")                       # lock before select→delete so a concurrent drainer can't race
            try:
                rows = c.execute(f"SELECT {','.join(cols)} FROM lane_queue "
                                 "WHERE state IN ('done','failed') AND updated_ts < ?", (cutoff,)).fetchall()
                if not rows:
                    c.execute("COMMIT")
                    return {"archived": 0, "deleted": 0}
                with open(archive_path, "a") as f:            # append-only audit log — archive BEFORE delete
                    for r in rows:
                        f.write(json.dumps(dict(zip(cols, r))) + "\n")
                c.executemany("DELETE FROM lane_queue WHERE id=?", [(r[0],) for r in rows])
                c.execute("COMMIT")
                return {"archived": len(rows), "deleted": len(rows), "archive": archive_path}
            except Exception:
                c.execute("ROLLBACK")
                raise
    except Exception as e:
        return {"archived": 0, "deleted": 0, "error": str(e)[:120]}


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
    idle = iters = 0
    while True:
        iters += 1
        if max_iters is not None and iters > int(max_iters):
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
        intent = rows[0]["intent"]
        # a leased batch is intent-uniform but may mix system/reasoning/sla_class — GROUP so bulk_delegate gets a
        # faithful (system, reasoning) per sub-batch AND a uniform sla_class, rather than silently applying the first
        # row's to all (no shortcut). sla_class flows to the governor so a 'batch' drain yields the realtime reserve.
        groups = {}
        for r in rows:
            groups.setdefault((r.get("system"), r.get("reasoning"), r.get("sla_class")), []).append(r)
        for (sys_, rea, sla_), grp in groups.items():
            results = lane_balance.bulk_delegate([g["task"] for g in grp], intent, system=sys_, reasoning=rea,
                                                 deadline_s=lease_s, sla_class=sla_)
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
    optimize_queue_db()          # refresh planner stats ONCE at drain completion (periodic, never per-op)
    return s
