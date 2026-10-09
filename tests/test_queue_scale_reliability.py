"""Item 3 — SCALE / VOLUME reliability of the durable queue, replaying the REAL failure MIX (Ash: "test scale and
volume at a unit time", "use real data and real failures").

The mix is REAL: tests/data/queue_failure_mix.json holds the outcome-class counts measured from ~/.spendguard/
vendor_calls.jsonl (19,161 real vendor_call results). This test draws VOLUME tasks in those real proportions and drives
every one through the REAL lane_queue (enqueue → lease → settle) in drain-shaped batches, then asserts the reliability
properties at scale:

  1. NO LOSS: after draining, zero rows are stuck pending/leased — every task reached a terminal state (done|failed).
  2. CLASS-AWARE at volume: every deterministic-class task fails FAST (attempt 1, no wasted retries); every ok task is
     done; transient (RETRYABLE) tasks recover when the fault clears within the retry-to-10 budget, else fail LOUD
     (never loop forever).
  3. THE RETRY-TO-10 LEVER: reports how many MORE transient tasks recover at the durable budget (10) than the old
     class-blind budget (3) would have — the concrete reliability gain from Item 3.
  4. THROUGHPUT: VOLUME tasks settle in bounded wall-time (the "unit time" scale metric), reported.

Deterministic: a fixed-seed RNG makes the draw + the transient-clear model reproducible, so the suite result is stable.
The clear model (a fault clears with probability P_CLEAR per attempt, a small fraction are persistently down) is an
EXPLICIT model of transient recovery — the real log records only final outcomes, not retry sequences — so the recovery
NUMBERS are labelled as modelled; the NO-LOSS, class-aware, and throughput properties do not depend on it.

Isolation: SPENDGUARD_HOME → tempfile.mkdtemp BEFORE importing spendguard (chunked_suite runs each test in its own
fresh subprocess).
"""
import json
import os
import random
import sys
import tempfile
import time

os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="spendguard-scale-")   # isolate BEFORE importing spendguard

from spendguard import lane_queue as lq, vendor_call as vc   # noqa: E402

# This test models transient RECOVERY by ATTEMPT COUNT (clear_at), driving retries via immediate re-lease in a
# zero-wait drain loop that exits the instant lease() returns empty. The F4 retry backoff (a transient failure defers
# re-lease RETRY_BACKOFF_S_DEFAULT seconds) is an ORTHOGONAL wall-clock mechanism — it would strand rows that are still
# in their backoff window when the loop exits (they are NOT lost: the daemon drain re-leases them once defer_until
# passes; see lane_queue.lease's `defer_until<=?` readiness filter). The backoff is proven in test_queue_review_fixes;
# here we neutralise it so the no-loss / class-aware / retry-to-10 properties are measured without that timing variable,
# exactly as the sibling queue tests (test_lane_queue, test_queue_class_aware_retry, test_queue_park_saturated) do.
lq.RETRY_BACKOFF_S_DEFAULT = 0.0

VOLUME = 2000                 # tasks driven through the queue (scale)
LEASE_BATCH = 100             # drain-shaped: lease this many per round
SEED = 20260927              # fixed → reproducible draw + clear model
P_CLEAR = 0.7                 # modelled: a transient fault clears with this prob per attempt (most clear in 1–3 tries)
DOWN_FRACTION = 0.03          # modelled: this fraction of transient tasks are persistently down (never clear → exhaust)
OLD_BLIND_BUDGET = 3          # the pre-Item-3 class-blind attempt budget, for the improvement metric
MIN_THROUGHPUT = 100.0        # settles/sec floor (very conservative; SQLite/WAL does thousands — guards a real stall)
_DETERMINISTIC = (vc.TRUNCATED, vc.DEADLINE_EXCEEDED, vc.SCHEMA_VIOLATION, vc.REFUSED, vc.EMPTY,
                  vc.PAYLOAD_REJECTED, vc.UNFUNDED, vc.PREFLIGHT_UNMET)


class Checks:
    def __init__(self):
        self.fails = 0

    def ck(self, label, cond, extra=""):
        if not cond:
            self.fails += 1
        print(f"  [{'OK' if cond else 'FAIL'}] {label}{('  — ' + extra) if extra and not cond else ''}")


def _load_real_mix():
    p = os.path.join(os.path.dirname(__file__), "data", "queue_failure_mix.json")
    with open(p) as fh:
        return json.load(fh)["counts"]


def _draw_classes(counts, n, rng):
    kinds = list(counts.keys())
    weights = [counts[k] for k in kinds]
    return rng.choices(kinds, weights=weights, k=n)


def _clear_at(rng):
    """The attempt on which a transient fault clears (modelled). A DOWN_FRACTION never clears (999 → exhausts the
    budget). Otherwise geometric-ish: keep failing while rng < (1 - P_CLEAR)."""
    if rng.random() < DOWN_FRACTION:
        return 999
    a = 1
    while rng.random() > P_CLEAR:
        a += 1
        if a > 50:            # numeric guard; at P_CLEAR=0.7 this is astronomically unlikely
            break
    return a


def main():
    c = Checks()
    counts = _load_real_mix()
    rng = random.Random(SEED)
    classes = _draw_classes(counts, VOLUME, rng)

    # per-task plan, keyed by the queue row id after enqueue
    plan = {}                 # rid -> {"cls":..., "clear_at":...}
    n_ok = n_det = n_ret = 0
    ret_clear_at = []
    for i, cls in enumerate(classes):
        # DISTINCT task text per item — these model VOLUME independent work items; identical text would (correctly)
        # content-address-dedup to a single row (see test_queue_content_addressed_dedup), collapsing the volume.
        rid = lq.enqueue("scale-repro", "task-%d" % i)
        entry = {"cls": cls, "clear_at": None}
        if cls == vc.OK:
            n_ok += 1
        elif cls in vc.RETRYABLE:
            n_ret += 1
            entry["clear_at"] = _clear_at(rng)
            ret_clear_at.append(entry["clear_at"])
        else:
            n_det += 1
        plan[rid] = entry

    t0 = time.time()
    rounds = 0
    settles = 0
    while True:
        leased = lq.lease(LEASE_BATCH)
        if not leased:
            break
        rounds += 1
        for r in leased:
            rid, att = r["id"], None
            e = plan.get(rid)
            # current attempt number = the row's attempts after this lease (lease already incremented it)
            att = _attempts(rid)
            if e["cls"] == vc.OK:
                lq.settle(rid, {"text": "ok", "lane": "test"})
            elif e["cls"] in vc.RETRYABLE:
                if att >= e["clear_at"]:
                    lq.settle(rid, {"text": "recovered", "lane": "test"})     # the transient fault cleared → success
                else:
                    lq.settle(rid, {"error": "%s (transient)" % e["cls"], "outcome": e["cls"]})  # still failing → retry
            else:
                lq.settle(rid, {"error": "%s (deterministic)" % e["cls"], "outcome": e["cls"]})  # fail fast
            settles += 1
        if rounds > VOLUME * (lq.MAX_ATTEMPTS_DEFAULT + 2):     # safety: can never legitimately exceed this
            c.ck("drain terminated (no runaway)", False, "rounds=%d" % rounds)
            break
    elapsed = time.time() - t0

    depth = lq.queue_depth()
    done, failed = depth.get("done", 0), depth.get("failed", 0)
    stuck = depth.get("pending", 0) + depth.get("leased", 0) + depth.get("queued_batch", 0)

    # 1. NO LOSS — everything terminal
    c.ck("no loss: 0 stuck (pending/leased/queued_batch) after drain", stuck == 0, str(depth))
    c.ck("accounting: done + failed == VOLUME", done + failed == VOLUME, "done=%d failed=%d N=%d" % (done, failed, VOLUME))

    # 2. CLASS-AWARE at volume: deterministic all failed-fast; ok all done; transient recover-or-loud-fail
    det_failed_fast = _count_failed_at_attempt(plan, 1, _DETERMINISTIC)
    c.ck("every deterministic task failed FAST at attempt 1", det_failed_fast == n_det,
         "failed_fast=%d n_det=%d" % (det_failed_fast, n_det))
    recovered = sum(1 for e in plan.values() if e["cls"] in vc.RETRYABLE and e["clear_at"] <= lq.MAX_ATTEMPTS_DEFAULT)
    ret_done = _count_state(plan, "done", vc.RETRYABLE)
    ret_failed = _count_state(plan, "failed", vc.RETRYABLE)
    c.ck("transient tasks: recovered→done, exhausted→failed, none stuck",
         ret_done == recovered and (ret_done + ret_failed) == n_ret,
         "ret_done=%d recovered=%d ret_failed=%d n_ret=%d" % (ret_done, recovered, ret_failed, n_ret))

    # 3. THE RETRY-TO-10 LEVER — extra recoveries vs the old class-blind budget of 3
    recover_at_10 = sum(1 for ca in ret_clear_at if ca <= lq.MAX_ATTEMPTS_DEFAULT)
    recover_at_3 = sum(1 for ca in ret_clear_at if ca <= OLD_BLIND_BUDGET)
    extra = recover_at_10 - recover_at_3
    c.ck("retry-to-10 recovers at least as many transient as retry-3 (the reliability lever)",
         recover_at_10 >= recover_at_3, "at10=%d at3=%d" % (recover_at_10, recover_at_3))

    # 4. THROUGHPUT
    rate = settles / elapsed if elapsed > 0 else 0.0
    c.ck("throughput floor met (queue scales)", rate >= MIN_THROUGHPUT, "%.0f settles/s" % rate)

    print("\n  scale summary (REAL mix, N=%d): ok=%d transient=%d deterministic=%d" % (VOLUME, n_ok, n_ret, n_det))
    print("    terminal: done=%d failed=%d stuck=%d  | rounds=%d settles=%d  %.2fs  (%.0f settles/s)"
          % (done, failed, stuck, rounds, settles, elapsed, rate))
    print("    transient recovery: %d/%d reached done via retry-to-%d  (retry-3 would recover %d → +%d MORE from Item 3)"
          % (ret_done, n_ret, lq.MAX_ATTEMPTS_DEFAULT, recover_at_3, extra))
    reliability = 100.0 * done / VOLUME
    print("    modelled end-state reliability: %.2f%% done  (deterministic %d correctly fail-fast, not retried)"
          % (reliability, n_det))

    print(f"\n{'[FAIL]' if c.fails else 'OK'} test_queue_scale_reliability: {c.fails} failure(s)")
    return 1 if c.fails else 0


def _attempts(rid):
    with lq._queue_op() as conn:
        row = conn.execute("SELECT attempts FROM lane_queue WHERE id=?", (rid,)).fetchone()
    return row[0] if row else 0


def _count_state(plan, state, class_filter):
    ids = [rid for rid, e in plan.items() if e["cls"] in class_filter]
    if not ids:
        return 0
    with lq._queue_op() as conn:
        rows = conn.execute("SELECT id, state FROM lane_queue WHERE state=?", (state,)).fetchall()
    hit = {r[0] for r in rows}
    return sum(1 for rid in ids if rid in hit)


def _count_failed_at_attempt(plan, attempt, class_filter):
    ids = [rid for rid, e in plan.items() if e["cls"] in class_filter]
    if not ids:
        return 0
    with lq._queue_op() as conn:
        rows = conn.execute("SELECT id, attempts FROM lane_queue WHERE state='failed'").fetchall()
    att = {r[0]: r[1] for r in rows}
    return sum(1 for rid in ids if att.get(rid) == attempt)


if __name__ == "__main__":
    sys.exit(main())
