"""dedup_pending_against_done enforces content-addressed dedup at the DRAIN (consumer side), so re-buy is prevented
even when the ENQUEUER runs OLDER code that inserts NULL-fingerprint rows bypassing enqueue-time dedup. Measured
2026-10-10: 371 pending rows were all NULL-fp (enqueued post-restart by long-lived pre-0.12.16 processes in other
venvs), 11 of them duplicating a done result — enqueue-time dedup alone could not close the leak. This locks in:
  • BACKFILL — a NULL-fingerprint row gets its fingerprint computed from its OWN columns;
  • SETTLE-FROM-DONE — a pending row whose fingerprint already has a completed result is settled from it ($0), never
    re-executed; a genuinely-new row is only fingerprinted and stays pending.
Offline, isolated HOME, no network, no LLM."""
import os
import sys
import tempfile

os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="sg-dedupcs-")
os.environ.setdefault("SPENDGUARD_TEST_ISOLATED", "1")
os.environ.setdefault("SPENDGUARD_NO_AUTOINSTALL", "1")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from spendguard import lane_queue as lq  # noqa: E402

fails = []
def ck(label, cond):
    print(f"  [{'OK' if cond else 'FAIL'}] {label}")
    if not cond:
        fails.append(label)

def _row(rid):
    with lq._queue_op() as c:
        r = c.execute("SELECT state, lane, billed, fingerprint, result FROM lane_queue WHERE id=?", (rid,)).fetchone()
    return r

def _insert_nullfp(intent, task, state, result=None):
    """Simulate an OLD-enqueuer insert: a row with NO fingerprint (bypasses enqueue-time dedup)."""
    now = lq._iso(lq._utcnow())
    with lq._queue_op() as c:
        cur = c.execute("INSERT INTO lane_queue(intent,task,state,attempts,max_attempts,created_ts,updated_ts,result) "
                        "VALUES(?,?,?,0,3,?,?,?)", (intent, task, state, now, now, result))
        c.commit()
        return cur.lastrowid

I = "legacy-intent"
# a DONE row (has a result) + a PENDING duplicate of it + a genuinely-new pending — all NULL-fingerprint
done_id = _insert_nullfp(I, "task-A", "done", result='{"text": "answer-A", "lane": "codex"}')
dup_id = _insert_nullfp(I, "task-A", "pending")                 # same (intent,task) as the done row → should settle $0
new_id = _insert_nullfp(I, "task-B", "pending")                 # no done result → only fingerprinted, stays pending

ck("seeded rows start with NULL fingerprint (simulating an old enqueuer)",
   _row(done_id)[3] is None and _row(dup_id)[3] is None and _row(new_id)[3] is None)

res = lq.dedup_pending_against_done()
print(f"  dedup result: {res}")

# BACKFILL: every row now has a fingerprint computed from its columns
ck("all rows fingerprinted after backfill", all(_row(r)[3] for r in (done_id, dup_id, new_id)))
ck("done and its duplicate share a fingerprint; the new task differs",
   _row(done_id)[3] == _row(dup_id)[3] and _row(new_id)[3] != _row(done_id)[3])
ck("fingerprinted count == 3", res.get("fingerprinted") == 3)

# SETTLE-FROM-DONE: the duplicate is settled from the done result, $0, never executed
drow = _row(dup_id)
ck("duplicate pending settled to done", drow[0] == "done")
ck("settled from the existing result (content-addressed-dedup, billed=0)",
   drow[1] == "content-addressed-dedup" and drow[2] == 0)
ck("settled row carries the done row's result", drow[4] == _row(done_id)[4])
ck("settled count == 1", res.get("settled") == 1)

# the genuinely-new row is only fingerprinted — it STAYS pending for the drain to actually run
ck("genuinely-new row stays pending (fingerprinted, not settled)", _row(new_id)[0] == "pending")

# idempotent: a second pass settles nothing more
res2 = lq.dedup_pending_against_done()
ck("second pass is a no-op (idempotent)", res2.get("settled") == 0 and res2.get("fingerprinted") == 0)

# ── LEASE-level dedup: two PENDING twins of one fingerprint are never leased together (same-batch re-buy) ──
# High priority so lease() (intent-uniform, highest-priority-first) targets THIS intent over the backlog of earlier
# sub-tests, and a batch of 10 grabs all three rows at once.
J = "lease-dedup-intent"
t1 = lq.enqueue(J, "twin task", dedup=False, priority=100)   # two independent pending rows of identical content
t2 = lq.enqueue(J, "twin task", dedup=False, priority=100)
solo = lq.enqueue(J, "solo task", dedup=False, priority=100)
ck("seeded 2 twins + 1 solo as distinct rows", len({t1, t2, solo}) == 3)
leased = lq.lease(10)                                  # batch big enough to grab all three
leased_ids = {r["id"] for r in leased}
leased_twin = leased_ids & {t1, t2}
ck("exactly ONE of the two twins was leased (not both) — no same-batch re-buy", len(leased_twin) == 1)
ck("the solo (distinct fingerprint) WAS leased alongside it", solo in leased_ids)
# the un-leased twin stays pending; settling the leased twin coalesces it from the result ($0)
leased_twin_id = leased_twin.pop()
other_twin = (t2 if leased_twin_id == t1 else t1)
ck("the un-leased twin is still pending", _row(other_twin)[0] == "pending")
lq.settle(leased_twin_id, {"text": "twin answer", "lane": "codex", "billed": True})
otw = _row(other_twin)
ck("settling the leased twin coalesced the pending twin to done ($0)",
   otw[0] == "done" and otw[1] == "content-addressed-dedup" and otw[2] == 0)

print(f"\n{'[FAIL]' if fails else 'OK'} test_queue_dedup_consumer_side: {len(fails)} failure(s)")
sys.exit(1 if fails else 0)
