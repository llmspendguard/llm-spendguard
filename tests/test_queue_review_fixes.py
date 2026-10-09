"""Guards for the correctness fixes a 4-provider review surfaced in the lane_queue drain/batch/settle state machine
(2026-10-09). A failure here BILLS the metered API, so these are money-critical. Flip any fix back and the matching
check fails:
  F1 — a SUBMITTED batch (batch_id present) is NEVER also run realtime, even if mark_batched tagged 0 rows (double-spend).
  F2 — settle() STATE-GUARDS the write, so a late batch result can't overwrite a row realtime already settled (double-bill).
  F4 — a TRANSIENT-failed retry gets a defer_until backoff (not an instant re-lease churn to failed).
  F7 — a 'queued_batch' reason with NO handle PARKS for re-mark, never realtime-retries (batch may be billed).
Offline, isolated HOME, no provider calls (bulk_delegate / submit_offload monkeypatched for the drain case)."""
import os
import sys
import json
import sqlite3
import tempfile

os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="sg-qrf-")
os.environ.setdefault("SPENDGUARD_TEST_ISOLATED", "1")
os.environ.setdefault("SPENDGUARD_NO_AUTOINSTALL", "1")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from spendguard import lane_queue as lq, vendor_call as vc  # noqa: E402

fails = []
def ck(label, cond):
    print(f"  [{'OK' if cond else 'FAIL'}] {label}")
    if not cond:
        fails.append(label)

def _state(rid):
    c = sqlite3.connect(lq.config.lane_queue_db_path())
    try:
        r = c.execute("SELECT state, attempts, parks, defer_until FROM lane_queue WHERE id=?", (rid,)).fetchone()
    finally:
        c.close()
    return r
def _result(rid):
    c = sqlite3.connect(lq.config.lane_queue_db_path())
    try:
        r = c.execute("SELECT result FROM lane_queue WHERE id=?", (rid,)).fetchone()
    finally:
        c.close()
    return json.loads(r[0]) if r and r[0] else {}
def _lease_one(intent):
    return next((r for r in lq.lease(1) if r["intent"] == intent), None)

# ── F2: settle() state guard — a row only settles from leased/queued_batch; a second settle can't overwrite ──
rid = lq.enqueue("f2", "t")
r = _lease_one("f2")
ck("F2 first settle (leased→done) succeeds", lq.settle(r["id"], {"text": "ok", "lane": "codex"}) is True)
ck("F2 row is done", _state(rid)[0] == "done")
ck("F2 a SECOND settle on the now-done row is refused (no overwrite/double-count)",
   lq.settle(rid, {"text": "STALE", "lane": "gemini"}) is False)
ck("F2 the done row was NOT overwritten by the stale settle", _result(rid).get("text") == "ok" and _state(rid)[0] == "done")

# ── F4: transient-failed retry backs off (defer_until set), not an instant re-lease ──
lq.RETRY_BACKOFF_S_DEFAULT = 30.0                     # ensure a real backoff for this check
rid = lq.enqueue("f4", "t")
r = _lease_one("f4")
lq.settle(r["id"], {"error": "boom", "outcome": vc.OVERLOADED})   # transient → retry
st = _state(rid)
ck("F4 transient failure → pending (retry)", st[0] == "pending")
ck("F4 the retry has a defer_until backoff (not instantly re-leasable)", bool(st[3]))
ck("F4 lease() does NOT immediately return the deferred row", _lease_one("f4") is None)

# ── F7: 'queued_batch' reason with NO handle → PARK (pending+defer, attempt refunded), never realtime-retry ──
rid = lq.enqueue("f7", "t")
r = _lease_one("f7")
before_attempts = _state(rid)[1]
lq.settle(r["id"], {"error": "offload glitch", "reason": "queued_batch"})   # queued_batch reason, NO 'batch' handle
st = _state(rid)
ck("F7 parked to pending (not failed, not realtime-retried as transient)", st[0] == "pending")
ck("F7 a defer_until was set (await re-mark)", bool(st[3]))
ck("F7 the attempt was REFUNDED (not burned on a billed batch)", st[1] <= before_attempts)

# ── F1: a submitted batch (batch_id) with marked=0 must NOT run realtime (the CRITICAL double-spend) ──
lq.enqueue_many("f1", ["a", "b", "c"], priority=0)
called = {"bulk_delegate": 0}
import spendguard.lane_balance as _lb  # noqa: E402
import spendguard.queue_planner as _qp  # noqa: E402
import spendguard.batch_tracker as _bt  # noqa: E402
_orig_bd, _orig_so, _orig_sh = _lb.bulk_delegate, _bt.submit_offload, _qp.should_offload
def _spy_bulk(tasks, intent, **kw):
    called["bulk_delegate"] += len(tasks)             # record any realtime run
    return [{"text": "x", "lane": "codex"} for _ in tasks]
def _fake_offload(intent, rows, batch_model, **kw):
    return {"batch_id": "batch_TEST", "marked": 0}     # batch SUBMITTED (billed) but tagged 0 rows (the F1 trap)
_orig_qcfg = lq._qcfg
_lb.bulk_delegate = _spy_bulk
_bt.submit_offload = _fake_offload
_qp.should_offload = lambda intent, n: {"batch_model": "gpt-5.1", "provider": "openai"}
lq._qcfg = lambda name, default: 1 if name == "queue_planner_autobatch" else _orig_qcfg(name, default)  # force autobatch
try:
    lq.drain(batch=10, idle_rounds=1, max_iters=3)
finally:
    _lb.bulk_delegate, _bt.submit_offload, _qp.should_offload, lq._qcfg = _orig_bd, _orig_so, _orig_sh, _orig_qcfg
ck("F1 a submitted batch (marked=0) did NOT fall through to realtime bulk_delegate (no double-spend)",
   called["bulk_delegate"] == 0)

print(f"\n{'[FAIL]' if fails else 'OK'} test_queue_review_fixes: {len(fails)} failure(s)")
sys.exit(1 if fails else 0)
