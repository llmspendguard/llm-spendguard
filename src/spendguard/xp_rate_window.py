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

REFUSE ON INFRA AND RATE: an unavailable shared store cannot prove admission and raises RateDeadline with the
infrastructure reason. Operational errors retry within the deadline; exhausted capacity also refuses at the deadline.
"""
import math
import os
import sqlite3
import time

from . import config

_XP_RATE_KEY = "xp_rate"
_MIN_WAIT_S = 0.02        # floor on a recomputed wait so an over-limit boundary (wait≈0) can never busy-loop


class RateDeadline(Exception):
    """The shared rate window could not admit this call: deadline exhausted or shared-store infrastructure failed."""


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
    rows, checks EVERY window, and either RECORDS the admission (returns (True, 0.0)) or returns (False, wait_s) with
    the time until the binding window frees. Pure given `now` — the unit test drives it with a fake clock for
    deterministic window math."""
    est = max(0, int(math.ceil(est or 0)))                       # D: round UP (never truncate a fractional est to 0)
    max_w = max((w for (w, _, _) in limits), default=60.0)
    c.execute("DELETE FROM xp_rate WHERE at < ?", (now - max_w,))  # G: GLOBAL prune of expired rows (any key) — no leak
    any_over = False
    worst_wait = 0.0
    for (w, maxc, maxt) in limits:
        rows = c.execute("SELECT at, tokens FROM xp_rate WHERE k=? AND at >= ?", (key, now - w)).fetchall()
        over = False
        if maxc is not None and len(rows) + 1 > maxc:
            over = True
        if maxt is not None:
            existing = sum((r[1] or 0) for r in rows)            # F: NULL-safe token sum
            if existing + est > maxt:
                # A: a request that cannot fit even an EMPTY window (est >= maxt) runs ALONE — admit ONLY when the
                # window is empty of tokens (unavoidable, cannot split), and it is STILL recorded so the NEXT request
                # paces behind it. Otherwise it is over and waits. This closes the old bypass where every oversized
                # request was admitted unconditionally, defeating tpm for exactly the biggest callers.
                if not (est >= maxt and existing == 0):
                    over = True
        if over:
            any_over = True
            oldest = min((r[0] for r in rows), default=now)
            worst_wait = max(worst_wait, (oldest + w) - now)
    if any_over:                                                 # H: OVER always WAITS — never fall through to INSERT
        return (False, max(worst_wait, _MIN_WAIT_S))
    c.execute("INSERT INTO xp_rate(k, at, tokens) VALUES(?,?,?)", (key, now, est))
    return (True, 0.0)


def _commit_rate_window_reservation(key, limits, est_tokens, clock):
    """Execute only the local atomic attempt on the ledger writer."""
    with config.ledger_op(_XP_RATE_KEY, _ensure_xp_rate_schema) as connection:
        from .ledger_writer import require_ledger_writer_request_live
        require_ledger_writer_request_live()
        connection.execute("BEGIN IMMEDIATE")
        result = _try_reserve(connection, key, limits, est_tokens, clock())
        require_ledger_writer_request_live()
        connection.commit()
        return result


def reserve(key, limits, est_tokens=0, deadline_s=30.0, clock=time.time, sleep=time.sleep):
    """Reserve one admission for `key` under the multi-window `limits`, waiting (bounded by deadline_s) until the
    shared window has room. Returns seconds waited. Raises RateDeadline if the aggregate rate stays saturated past the
    deadline. A shared-store infrastructure failure raises RateDeadline with the complete reason. `clock`/`sleep` are injectable for tests."""
    if _xp_rate_off() or not limits:
        return 0.0
    from .ledger_read import effective_ledger_admission_deadline, LedgerReadError
    try:
        reservation_expiry = effective_ledger_admission_deadline(time.monotonic() + float(deadline_s))
    except LedgerReadError as error:
        raise RateDeadline(f"'{key}' shared rate reservation deadline exhausted") from error
    t0 = clock()

    def remaining_rate_window_budget():
        try:
            effective_ledger_admission_deadline(reservation_expiry)
        except LedgerReadError as error:
            raise RateDeadline(f"'{key}' shared rate reservation deadline exhausted") from error
        return min(float(deadline_s) - (clock() - t0), reservation_expiry - time.monotonic())

    while True:
        remaining = remaining_rate_window_budget()
        if remaining <= 0:
            raise RateDeadline("'%s' shared rate window saturated — deadline %.0fs exhausted" % (key, float(deadline_s)))
        try:
            from .ledger_writer import run_ledger_write
            from .ledger_read import ledger_request_scope
            with ledger_request_scope(deadline_s=remaining):
                ok, wait = run_ledger_write(
                    lambda: _commit_rate_window_reservation(key, limits, est_tokens, clock))
        except LedgerReadError as error:
            raise RateDeadline(f"'{key}' shared rate reservation deadline exhausted") from error
        except sqlite3.OperationalError:                 # C: transient lock/busy — RETRY within the deadline, do NOT
            sleep(min(_MIN_WAIT_S, max(0.0, remaining_rate_window_budget())))
            continue
        except sqlite3.Error as error:
            raise RateDeadline(f"xp_rate_window.reserve: '{key}' shared rate store failed "
                               f"({type(error).__name__}: {error})") from error
        if ok:
            return clock() - t0
        sleep(min(wait + 0.001, max(0.0, remaining_rate_window_budget())))
