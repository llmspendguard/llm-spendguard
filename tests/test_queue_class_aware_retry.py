"""Item 3 — CLASS-AWARE durable queue retry: settle() spends the retry-to-10 budget ONLY on TRANSIENT outcomes
(vendor_call.RETRYABLE — transport_error / overloaded) and fails a DETERMINISTIC one FAST (payload_rejected / refused
/ unfunded / deadline / truncated / empty), instead of the old behavior that retried every failure class equally.

Offline + deterministic ($0, no provider call): drives the REAL lane_queue (enqueue → lease → settle) with synthetic
result dicts carrying a vendor_call outcome KIND, and asserts the state machine. This is the anti-amnesia guard for the
fix — flip settle back to class-blind, or drop the outcome stamp, and these fail.

Isolation: SPENDGUARD_HOME is redirected to a throwaway tempfile.mkdtemp BEFORE importing spendguard (the enforcement
test accepts this in place of the execv re-exec; chunked_suite runs each test in its own fresh subprocess, so nothing
imports spendguard before this line).
"""
import os, sys, tempfile

os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="spendguard-retry-")   # isolate BEFORE importing spendguard

from spendguard import lane_queue as lq, vendor_call as vc   # noqa: E402
lq.RETRY_BACKOFF_S_DEFAULT = 0.0   # this test drives retry COUNT/class via immediate re-lease; the F4 retry backoff
#                                    (defer before re-lease) is guarded separately in test_queue_review_fixes.py


class Checks:
    def __init__(self):
        self.fails = 0

    def ck(self, label, cond, extra=""):
        if not cond:
            self.fails += 1
        print(f"  [{'OK' if cond else 'FAIL'}] {label}{('  — ' + extra) if extra and not cond else ''}")


def _row(rid):
    with lq._queue_op() as c:
        r = c.execute("SELECT state, attempts FROM lane_queue WHERE id=?", (rid,)).fetchone()
    return (r[0], r[1]) if r else (None, None)


def _lease(intent):
    """Lease the one pending row of `intent` (temp home holds only this test's rows, one intent pending at a time)."""
    rows = lq.lease(1)
    return next((r for r in rows if r["intent"] == intent), None)


def _run_to_terminal(intent, outcome, max_rounds=15):
    """Enqueue one task, then repeatedly lease + settle it with `outcome` until it leaves pending. Returns
    (final_state, final_attempts, rounds_that_ran)."""
    rid = lq.enqueue(intent, "task for %s" % intent)
    rounds = 0
    for _ in range(max_rounds):
        r = _lease(intent)
        if not r:
            break                                       # no longer leasable → terminal (failed/done)
        rounds += 1
        result = {"error": "%s simulated" % outcome, "outcome": outcome} if outcome != vc.OK \
            else {"text": "ok", "lane": "test"}
        lq.settle(r["id"], result)
    st, att = _row(rid)
    return st, att, rounds


def main():
    c = Checks()
    c.ck("MAX_ATTEMPTS_DEFAULT is 10 (durable retry-to-10)", lq.MAX_ATTEMPTS_DEFAULT == 10,
         str(lq.MAX_ATTEMPTS_DEFAULT))

    # ── TRANSIENT (RETRYABLE) → retries the FULL budget, then fails only after 10 attempts ──
    for oc in (vc.OVERLOADED, vc.TRANSPORT_ERROR):
        st, att, rounds = _run_to_terminal("transient-%s" % oc, oc)
        c.ck("transient %s: retried to the budget then failed" % oc,
             st == "failed" and att == lq.MAX_ATTEMPTS_DEFAULT and rounds == lq.MAX_ATTEMPTS_DEFAULT,
             "state=%s attempts=%s rounds=%s" % (st, att, rounds))

    # ── DETERMINISTIC → fails FAST on the FIRST attempt, budget untouched ──
    for oc in (vc.PAYLOAD_REJECTED, vc.REFUSED, vc.UNFUNDED, vc.DEADLINE_EXCEEDED, vc.TRUNCATED,
               vc.EMPTY, vc.PREFLIGHT_UNMET, vc.SCHEMA_VIOLATION, vc.GATE_REFUSED):
        st, att, rounds = _run_to_terminal("determ-%s" % oc, oc)
        c.ck("deterministic %s: failed fast at attempt 1 (no wasted retries)" % oc,
             st == "failed" and att == 1 and rounds == 1, "state=%s attempts=%s rounds=%s" % (st, att, rounds))

    # ── SUCCESS still settles done ──
    st, att, rounds = _run_to_terminal("success-path", vc.OK)
    c.ck("success → done on first attempt", st == "done" and att == 1 and rounds == 1,
         "state=%s attempts=%s" % (st, att))

    # ── ABSENT outcome → treated as retryable (conservative; matches pre-class behavior for an unclassified row) ──
    # Run LAST: it deliberately leaves the row PENDING, which would otherwise out-rank (older id) a later sub-test's
    # row at lease time.
    rid = lq.enqueue("absent-outcome", "task")
    r = _lease("absent-outcome")
    lq.settle(r["id"], {"error": "some failure with no outcome stamp"})     # no 'outcome' key
    st, att = _row(rid)
    c.ck("absent outcome → retried (back to pending), not failed-fast", st == "pending" and att == 1,
         "state=%s attempts=%s" % (st, att))

    print(f"\n{'[FAIL]' if c.fails else 'OK'} test_queue_class_aware_retry: {c.fails} failure(s)")
    return 1 if c.fails else 0


if __name__ == "__main__":
    sys.exit(main())
