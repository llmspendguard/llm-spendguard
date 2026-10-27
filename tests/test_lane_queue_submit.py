"""GUARD — lane_queue.submit(): the durable-queue FRONT DOOR with a sync fast-path (Layer 2a).

Every request enters the durable queue with a priority + service class + SLA, and either runs INLINE now (wait=True,
the sync fast-path) or is left for the drain daemon (wait=False). Pins:
  (a) wait=True runs every task through the engine and RETURNS a result per task IN ORDER, durable=True;
  (b) the sync rows are durably recorded and SETTLE to 'done' (observable + crash-recoverable), owned by us from the
      start (state='leased' at enqueue) so the daemon never races them;
  (c) every row carries its sla_class + an absolute SLA deadline_ts (sla_s -> deadline);
  (d) wait=False enqueues 'pending' and returns {queued, results:None} for the daemon (async/batch backfill).
Hermetic: lane_balance.bulk_delegate stubbed (the engine is exercised elsewhere); the REAL durable queue in an
isolated home; no network, no spend.
"""
import os
import sqlite3
import sys
import tempfile

os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="sg-submit-")
os.environ.setdefault("SPENDGUARD_TEST_ISOLATED", "1")
os.environ.setdefault("SPENDGUARD_NO_AUTOINSTALL", "1")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from _script_checks import record_script_check
from spendguard import lane_queue, lane_balance, config   # noqa: E402

fails = []




TASKS = ["t1", "t2", "t3"]


def _fake_bulk(tasks, intent, **kw):
    # one served row per task, in order (submit maps results onto its rows by position)
    return [{"text": "ans:%s" % t, "lane": "gemini", "billed": False, "error": None} for t in tasks]


_saved = lane_balance.bulk_delegate
lane_balance.bulk_delegate = _fake_bulk

try:
    # ── (a)+(b): wait=True sync fast-path — runs inline, returns results in order, durably recorded + settled done ──
    print("-- (a)/(b) wait=True: sync fast-path runs inline, returns results in order, rows settle to done --")
    out = lane_queue.submit("test:intent", list(TASKS), sla_class="realtime", sla_s=30)
    record_script_check(fails, "wait=True returns durable=True (every request recorded)", out.get("durable") is True)
    record_script_check(fails, "wait=True returns a result per task IN ORDER",
       [r.get("text") for r in (out.get("results") or [])] == ["ans:%s" % t for t in TASKS])
    record_script_check(fails, "wait=True recorded one durable row per task", len(out.get("queued") or []) == len(TASKS))
    depth = lane_queue.queue_depth()
    record_script_check(fails, "the sync rows SETTLED to 'done' (durable + observable + crash-recoverable)", depth.get("done", 0) == len(TASKS))

    # ── (c): every row carries the service class + an SLA deadline ──
    print("\n-- (c) every row carries sla_class + an SLA deadline_ts --")
    c = sqlite3.connect(config.lane_queue_db_path())   # the queue lives in its OWN db, not the spend.db ledger
    rows = c.execute("SELECT sla_class, deadline_ts FROM lane_queue WHERE intent='test:intent'").fetchall()
    record_script_check(fails, "every sync row carries sla_class='realtime'", bool(rows) and all(r[0] == "realtime" for r in rows))
    record_script_check(fails, "every sync row carries an absolute SLA deadline_ts (sla_s=30 -> a deadline)", all(r[1] for r in rows))

    # ── (d): wait=False async — enqueue pending, no inline run ──
    print("\n-- (d) wait=False: enqueue 'pending' for the daemon (async/batch backfill), no inline run --")
    out2 = lane_queue.submit("batch:intent", ["b1", "b2"], sla_class="batch", wait=False,
                             priority=lane_queue.PRIORITY_BULK)
    record_script_check(fails, "wait=False returns queued ids + results=None (async)", len(out2.get("queued") or []) == 2 and out2.get("results") is None)
    record_script_check(fails, "wait=False acknowledges accepted work with count, handles, processing, and query instructions",
       out2.get("accepted") is True and out2.get("queued_count") == 2 and out2.get("will_process") is True
       and out2.get("status_handles") == out2.get("queued") and "--id" in out2.get("status_query", ""))
    d2 = lane_queue.queue_depth()
    record_script_check(fails, "wait=False rows are 'pending' (the daemon drains them on spare capacity)", d2.get("pending", 0) == 2)
finally:
    lane_balance.bulk_delegate = _saved

print(f"\n{'[FAIL]' if fails else 'OK'} test_lane_queue_submit: {len(fails)} failure(s)")
sys.exit(1 if fails else 0)
