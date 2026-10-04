"""CROSS-PROCESS sliding-window RATE reservation — the twin of dispatch._acquire_xp (which bounds cross-process
CONCURRENCY via flock slots) for the axis concurrency cannot bound: REQUESTS/SECOND and TOKENS/MINUTE.

WHY (the 429-storm root cause + the Prompt 9 panel's #1): the in-process `_Bucket` paces rpm/tpm PER PROCESS and its
token bucket STARTS FULL, so (a) a burst up to rpm is admitted unpaced within one process, and (b) K processes each
pace to 100% of the vendor cap ⇒ aggregate K× over the wall. A concurrency cap (flock) does NOT fix this: at low
latency a few concurrent slots still yield unbounded requests/sec. This module reserves every metered admission from a
SHARED sqlite sliding window with NO burst allowance beyond an explicit per-second sub-cap, so aggregate egress across
ALL processes stays under the vendor's real limits.

MULTI-WINDOW (panel I17): a reservation must satisfy EVERY (window_s, max_count, max_tokens) limit simultaneously — a
1s sub-cap bounds the instantaneous stampede (the thing that 429'd 931× in the replay) AND a 60s cap bounds sustained
rpm/tpm. The shared store is the ledger db (same cross-process BEGIN IMMEDIATE lane_queue.lease relies on).

FAIL-OPEN ON INFRA, NOT ON RATE: if the shared store is unreadable/locked beyond the deadline we ADMIT (the in-process
bucket is still the per-process backstop) — we never block a caller because our own rate db broke. But being OVER the
rate is not an infra error: that WAITS (bounded by the deadline), which is the whole point.
"""
import math
import os
import time

from . import config

_XP_RATE_KEY = "xp_rate"


class RateDeadline(Exception):
    """The shared rate window could not admit this call within its deadline (aggregate vendor rate is saturated)."""


def _xp_rate_off():
    """Disabled by the global cross-process switch OR its own switch. Default ON — this is the storm fix."""
    return (os.environ.get("SPENDGUARD_DISPATCH_XP_OFF") == "1"
            or os.environ.get("SPENDGUARD_DISPATCH_XP_RATE_OFF") == "1")


def _ensure_xp_rate_schema(c):
    """Create the shared rate-event table + its (key, at) index. Idempotent + race-safe (CREATE IF NOT EXISTS only —
    no ALTER, so no additive-migration race). One row per admitted call: (key, wall-clock `at`, est tokens)."""
    c.execute("CREATE TABLE IF NOT EXISTS xp_rate(k TEXT NOT NULL, at REAL NOT NULL, tokens INTEGER DEFAULT 0)")
    c.execute("CREATE INDEX IF NOT EXISTS xp_rate_k_at ON xp_rate(k, at)")


def limits_for(rpm=0, tpm=0, per_second_burst=1.0):
    """Build the multi-window limit set from a vendor's rpm/tpm. Returns [(window_s, max_count, max_tokens), ...]:
      - (60s, rpm, None)        — sustained requests/minute (the vendor's PUBLISHED, hard limit)
      - (1s, ceil(rpm/60 * burst), None) — the per-second sub-cap that bounds the instantaneous stampede
      - (60s, None, tpm)        — sustained tokens/minute
    `per_second_burst` (>=1) is how much bunching to allow above the strict per-second AVERAGE. Default 1.0 = strict
    average (ceil(rpm/60)/s): this SMOOTHS a burst completely while still admitting the full rpm over the minute — the
    safest default, because only the per-MINUTE rpm is published, so assuming the per-second wall is rpm/60 can never
    overshoot it. Raise it only for a vendor known to tolerate short bursts. Empty when neither rpm nor tpm is known."""
    lims = []
    if rpm and rpm > 0:
        lims.append((60.0, int(rpm), None))
        lims.append((1.0, max(1, int(math.ceil(rpm / 60.0 * max(1.0, per_second_burst)))), None))
    if tpm and tpm > 0:
        lims.append((60.0, None, int(tpm)))
    return lims


def _try_reserve(c, key, limits, est, now):
    """ONE atomic attempt inside a BEGIN IMMEDIATE txn (the caller holds the cross-process write lock). Prunes expired
    rows, checks EVERY window, and either records the admission (returns (True, 0.0)) or returns (False, wait_s) with
    the time until the binding window's oldest event expires. Pure given `now` — the unit test drives it with a fake
    clock for deterministic window math."""
    max_w = max((w for (w, _, _) in limits), default=60.0)
    c.execute("DELETE FROM xp_rate WHERE k=? AND at < ?", (key, now - max_w))
    worst_wait = 0.0
    for (w, maxc, maxt) in limits:
        rows = c.execute("SELECT at, tokens FROM xp_rate WHERE k=? AND at >= ?", (key, now - w)).fetchall()
        over = False
        if maxc is not None and len(rows) + 1 > maxc:
            over = True
        # a call whose OWN est exceeds the token window can never fit — admit it (unavoidable) rather than wedge forever
        if maxt is not None and est < maxt and sum(r[1] for r in rows) + est > maxt:
            over = True
        if over:
            oldest = min((r[0] for r in rows), default=now)
            worst_wait = max(worst_wait, (oldest + w) - now)
    if worst_wait > 0:
        return (False, worst_wait)                       # do NOT insert; the prune still commits (keeps the table small)
    c.execute("INSERT INTO xp_rate(k, at, tokens) VALUES(?,?,?)", (key, now, int(max(0, est))))
    return (True, 0.0)


def reserve(key, limits, est_tokens=0, deadline_s=30.0, clock=time.time, sleep=time.sleep):
    """Reserve one admission for `key` under the multi-window `limits`, waiting (bounded by deadline_s) until the
    shared window has room. Returns seconds waited. Raises RateDeadline if the aggregate rate stays saturated past the
    deadline. Degrades to ADMIT (returns 0.0) on an INFRA failure of the shared store — never blocks a call because the
    rate db broke; the in-process bucket remains the per-process backstop. `clock`/`sleep` are injectable for tests."""
    if _xp_rate_off() or not limits:
        return 0.0
    t0 = clock()
    while True:
        now = clock()
        remaining = float(deadline_s) - (now - t0)
        if remaining <= 0:
            raise RateDeadline("'%s' shared rate window saturated — deadline %.0fs exhausted" % (key, float(deadline_s)))
        try:
            with config.ledger_op(_XP_RATE_KEY, _ensure_xp_rate_schema) as c:
                c.execute("BEGIN IMMEDIATE")             # cross-process write lock: check+insert is one atomic step
                ok, wait = _try_reserve(c, key, limits, est_tokens, now)
                c.commit()
        except Exception:
            return clock() - t0                          # INFRA failure → admit (fail-open on infra, not on rate)
        if ok:
            return clock() - t0
        sleep(min(wait + 0.001, remaining))
