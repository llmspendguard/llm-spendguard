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
import contextlib
import datetime
import json
import os
import time

from . import config

LEASE_S_DEFAULT = 300.0         # a leased task must settle within this window or it is reclaimed (worker presumed dead)
MAX_ATTEMPTS_DEFAULT = 3        # retry a task up to this many times before marking it `failed`
IDLE_ROUNDS_DEFAULT = 2         # foreground drain stops after this many consecutive EMPTY leases (queue drained)
IDLE_SLEEP_DEFAULT = 2.0        # seconds to wait between empty leases / overload re-checks (foreground + daemon)
_RESULT_CAP = 4000            # bytes of result JSON retained per row (audit/debug, not the whole payload)


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


def _queue_db():
    """The durable queue table. Named `_queue_db` (not `_db`) so it does not join the repo's 8-way `_db` collision —
    same reason lane_bandit uses `_bandit_db`. Forward-only additive migration (CREATE TABLE IF NOT EXISTS)."""
    import sqlite3
    c = sqlite3.connect(config.db_path(), timeout=15, check_same_thread=False)
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("""CREATE TABLE IF NOT EXISTS lane_queue(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        intent TEXT NOT NULL, task TEXT NOT NULL, system TEXT, reasoning TEXT,
        priority INTEGER DEFAULT 0,
        state TEXT DEFAULT 'pending',        -- pending | leased | done | failed
        lease_until TEXT, attempts INTEGER DEFAULT 0, max_attempts INTEGER DEFAULT 3,
        worker TEXT, result TEXT, lane TEXT, billed INTEGER DEFAULT 0,
        created_ts TEXT, updated_ts TEXT)""")
    # index the lease hot-path (pick highest-priority oldest pending) so a deep backlog stays cheap to poll.
    c.execute("CREATE INDEX IF NOT EXISTS lane_queue_pick ON lane_queue(state, priority DESC, id)")
    return c


def enqueue(intent, task, system=None, reasoning=None, priority=0, max_attempts=None):
    """Append ONE task. NEVER blocks and never runs it — that is the point: work is accepted at any utilization and
    sits `pending` until a drainer has lane capacity. Returns the row id, or None on error."""
    return (enqueue_many(intent, [task], system=system, reasoning=reasoning,
                         priority=priority, max_attempts=max_attempts) or [None])[0]


def enqueue_many(intent, tasks, system=None, reasoning=None, priority=0, max_attempts=None):
    """Append many tasks of ONE intent in a single transaction (a 6k-item backfill is one commit). Returns the new
    row ids in order; [] on error or empty input."""
    tasks = [t for t in (tasks or []) if t is not None]
    if not intent or not tasks:
        return []
    maxa = int(max_attempts if max_attempts is not None else _qcfg("queue_max_attempts", MAX_ATTEMPTS_DEFAULT))
    now = _iso(_utcnow())
    try:
        with contextlib.closing(_queue_db()) as c:
            cur = c.cursor()
            ids = []
            for t in tasks:
                cur.execute("INSERT INTO lane_queue(intent,task,system,reasoning,priority,state,attempts,"
                            "max_attempts,created_ts,updated_ts) VALUES(?,?,?,?,?, 'pending', 0, ?,?,?)",
                            (intent, t, system, reasoning, int(priority), maxa, now, now))
                ids.append(cur.lastrowid)
            c.commit()
            return ids
    except Exception:
        return []


def lease(n, worker=None, lease_s=None):
    """Atomically claim up to `n` pending tasks of the HIGHEST-priority pending intent (priority desc, then oldest),
    marking them `leased` with a fresh lease_until and attempts+1. First, within the SAME write lock, RECLAIMS
    expired leases (worker died): those under max_attempts return to `pending`, those at/over it become `failed`.
    Returns a list of {id,intent,task,system,reasoning} dicts (empty if nothing is pending). Never raises.

    Intent-UNIFORM by design: a batch shares one intent so bulk_delegate routes it with that intent's bandit arms;
    a higher-priority row of a DIFFERENT intent is simply picked up on the next round."""
    n = max(1, int(n))
    lease_s = float(lease_s if lease_s is not None else _qcfg("queue_lease_s", LEASE_S_DEFAULT))
    worker = worker or f"drain-{os.getpid()}"
    now_dt = _utcnow()
    now, until = _iso(now_dt), _iso(now_dt + datetime.timedelta(seconds=lease_s))
    try:
        with contextlib.closing(_queue_db()) as c:
            c.execute("BEGIN IMMEDIATE")                       # take the write lock BEFORE selecting → cross-process safe
            try:
                # reclaim expired leases: exhausted → failed, else → pending (retryable). attempts was already
                # incremented at lease time, so `attempts>=max_attempts` here means every allowed try is spent.
                c.execute("UPDATE lane_queue SET state='failed', updated_ts=? "
                          "WHERE state='leased' AND lease_until<? AND attempts>=max_attempts", (now, now))
                c.execute("UPDATE lane_queue SET state='pending', worker=NULL, updated_ts=? "
                          "WHERE state='leased' AND lease_until<? AND attempts<max_attempts", (now, now))
                top = c.execute("SELECT intent FROM lane_queue WHERE state='pending' "
                                "ORDER BY priority DESC, id ASC LIMIT 1").fetchone()
                if not top:
                    c.execute("COMMIT")
                    return []
                intent = top[0]
                rows = c.execute("SELECT id,intent,task,system,reasoning FROM lane_queue "
                                 "WHERE state='pending' AND intent=? ORDER BY priority DESC, id ASC LIMIT ?",
                                 (intent, n)).fetchall()
                for r in rows:
                    c.execute("UPDATE lane_queue SET state='leased', lease_until=?, attempts=attempts+1, "
                              "worker=?, updated_ts=? WHERE id=?", (until, worker, now, r[0]))
                c.execute("COMMIT")
                return [{"id": r[0], "intent": r[1], "task": r[2], "system": r[3], "reasoning": r[4]} for r in rows]
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
        with contextlib.closing(_queue_db()) as c:
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


def queue_depth():
    """{pending, leased, done, failed} counts — the 'is anything queued' view (parallel to dispatch.queue_state).
    Empty dict on error."""
    try:
        with contextlib.closing(_queue_db()) as c:
            rows = c.execute("SELECT state, COUNT(*) FROM lane_queue GROUP BY state").fetchall()
        out = {"pending": 0, "leased": 0, "done": 0, "failed": 0}
        for st, n in rows:
            out[st] = n
        return out
    except Exception:
        return {}


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
        # a leased batch is intent-uniform but may mix system/reasoning — GROUP so bulk_delegate gets a faithful
        # (system, reasoning) per sub-batch rather than silently applying the first row's to all (no shortcut).
        groups = {}
        for r in rows:
            groups.setdefault((r.get("system"), r.get("reasoning")), []).append(r)
        for (sys_, rea), grp in groups.items():
            results = lane_balance.bulk_delegate([g["task"] for g in grp], intent, system=sys_, reasoning=rea,
                                                 deadline_s=lease_s)
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
    return s
