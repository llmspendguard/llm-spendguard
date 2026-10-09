"""The lane_queue NEVER re-buys work it already has (content-addressed dedup). Measured 2026-10-09: with no dedup one
intent had ~20x content-duplicate rows (4,698 metered calls for ~239 unique tasks) — draining them would re-execute
finished work. This locks in:
  • enqueue coalesces an identical (intent,task,system,reasoning) task onto an existing non-failed row instead of
    inserting a re-execution — preferring a DONE row (reuse its result for $0) over an in-flight one;
  • duplicates WITHIN one enqueue_many call coalesce too;
  • settle coalesces every OTHER pending row of the same fingerprint from the result ($0) — closing the
    concurrent-enqueue race and draining pre-existing duplicates;
  • dedup=False forces an independent row (deliberate repeats);
  • a FAILED row is never coalesced onto (new work gets a fresh attempt); distinct inputs never collide.
Offline, isolated HOME, no network, no LLM."""
import os
import sys
import tempfile

os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="sg-cadedup-")
os.environ.setdefault("SPENDGUARD_TEST_ISOLATED", "1")
os.environ.setdefault("SPENDGUARD_NO_AUTOINSTALL", "1")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from spendguard import lane_queue as lq  # noqa: E402

fails = []
def ck(label, cond):
    print(f"  [{'OK' if cond else 'FAIL'}] {label}")
    if not cond:
        fails.append(label)

def _state(rid):
    with lq._queue_op() as c:
        r = c.execute("SELECT state, lane, billed FROM lane_queue WHERE id=?", (rid,)).fetchone()
    return (r[0], r[1], r[2]) if r else (None, None, None)

OK = {"text": "answer", "lane": "codex", "billed": False}

# ── 1. identical pending task coalesces onto the SAME row (no re-execution) ──
a = lq.enqueue("intent-x", "do the thing", system="S", reasoning="r")
b = lq.enqueue("intent-x", "do the thing", system="S", reasoning="r")
ck("identical pending task returns the SAME id (coalesced)", a == b and a is not None)
ck("only ONE pending row exists for the fingerprint", lq.queue_depth().get("pending", 0) == 1)

# ── 2. distinct inputs do NOT coalesce (different task/system/reasoning → different fingerprint) ──
c_task = lq.enqueue("intent-x", "do a DIFFERENT thing", system="S", reasoning="r")
c_sys = lq.enqueue("intent-x", "do the thing", system="DIFFERENT", reasoning="r")
c_rea = lq.enqueue("intent-x", "do the thing", system="S", reasoning="DIFFERENT")
ck("different task/system/reasoning get distinct ids", len({a, c_task, c_sys, c_rea}) == 4)

# ── 3. dedup against a DONE row: reuse its result for $0, no new pending row ──
lease = lq.lease(1)                                  # lease the oldest pending (id a)
lq.settle(a, OK)
ck("leased row settled to done", _state(a)[0] == "done")
d = lq.enqueue("intent-x", "do the thing", system="S", reasoning="r")   # same fingerprint as the now-DONE row
ck("enqueue of an already-DONE task returns the done row's id (reuse, $0)", d == a)
ck("no new pending row was created for the done-duplicate", d == a and _state(d)[0] == "done")

# ── 4. intra-batch dedup: duplicate tasks within ONE enqueue_many coalesce ──
ids = lq.enqueue_many("intent-y", ["p", "q", "p", "p", "q"], system=None, reasoning=None)
ck("enqueue_many returns ids 1:1 with inputs", len(ids) == 5)
ck("duplicate tasks in one batch share one id ('p' x3, 'q' x2 → 2 distinct)", len(set(ids)) == 2)
ck("'p' positions all map to one id", ids[0] == ids[2] == ids[3] and ids[1] == ids[4])

# ── 5. settle COALESCING: two pending rows of the same fingerprint (forced via dedup=False) → settling one
#       settles the other from the result for $0. High priority so lease() deterministically targets e1 over the
#       backlog of earlier sub-tests (lease is highest-priority-then-oldest); e1 gets leased, e2 stays pending. ──
e1 = lq.enqueue("intent-z", "coalesce me", dedup=False, priority=100)
e2 = lq.enqueue("intent-z", "coalesce me", dedup=False, priority=100)   # 2nd independent row (simulates the race)
ck("dedup=False created two distinct rows", e1 != e2)
leased = lq.lease(1)                                 # highest priority (100) → e1 (oldest of that tier); e2 stays pending
ck("lease targeted e1 (the high-priority row)", leased and leased[0]["id"] == e1)
lq.settle(e1, {"text": "shared answer", "lane": "codex", "billed": True})
st1, _, _ = _state(e1)
st2, lane2, billed2 = _state(e2)
ck("the leased row settled done", st1 == "done")
ck("the OTHER same-fingerprint pending row was coalesced to done", st2 == "done")
ck("the coalesced row is tagged content-addressed-dedup and billed=0", lane2 == "content-addressed-dedup" and billed2 == 0)

# ── 6. a FAILED row is NOT coalesced onto — new identical work gets a FRESH row (priority so lease targets f1) ──
f1 = lq.enqueue("intent-f", "will fail", max_attempts=1, priority=200)
leased_f = lq.lease(1)                               # highest priority (200) → f1
ck("lease targeted f1", leased_f and leased_f[0]["id"] == f1)
lq.settle(f1, {"error": "payload_rejected", "outcome": "payload_rejected"})   # deterministic → fails fast
ck("row failed fast", _state(f1)[0] == "failed")
f2 = lq.enqueue("intent-f", "will fail")             # same fingerprint as the FAILED row
ck("new enqueue does NOT coalesce onto a failed row (fresh attempt)", f2 != f1 and _state(f2)[0] == "pending")

print(f"\n{'[FAIL]' if fails else 'OK'} test_queue_content_addressed_dedup: {len(fails)} failure(s)")
sys.exit(1 if fails else 0)
