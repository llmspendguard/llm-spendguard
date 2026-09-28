"""C3b — the dedicated BATCH-JOB tracker lifecycle: register → poll (settle / stay-open / expire-fail-over / resume).

Offline + deterministic ($0, no provider call): fakes callio.batch_status + callio.collect_chat_tasks to drive the
async lifecycle and asserts every path — in_progress keeps the job open, completed settles its rows out-of-order, an
EXPIRED batch fails its rows OVER to the realtime retry ladder (the drain planner can re-batch them fresh), and a
'failing' job (a fail that crashed mid-move) RESUMES to 'failed' with no double-processing. Nothing is ever stuck or
lost; poll() never submits a new batch (no exactly-once hazard).
"""
import os, sys, tempfile

if not os.environ.get("SPENDGUARD_TEST_ISOLATED"):
    os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"
    os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="spendguard-bt-")
    os.execv(sys.executable, [sys.executable] + sys.argv)

from spendguard import lane_queue as lq, batch_tracker as bt, callio   # noqa: E402


class Checks:
    def __init__(self):
        self.fails = 0

    def ck(self, label, cond, extra=""):
        if not cond:
            self.fails += 1
        print(f"  [{'OK' if cond else 'FAIL'}] {label}{('  — ' + extra) if extra and not cond else ''}")


def _lease_ids(intent):
    return [r["id"] for r in lq.lease(10) if r["intent"] == intent]


def main():
    c = Checks()
    real_status, real_collect = callio.batch_status, callio.collect_chat_tasks
    try:
        # ---- a job that goes in_progress → completed → settled ----
        [lq.enqueue("summarize", "task %d" % i) for i in range(3)]
        s_ids = _lease_ids("summarize")
        lq.mark_batched(s_ids, "batch-1", "openai:gpt-5-nano")
        bt.register_batch("batch-1", "openai", "openai:gpt-5-nano", "summarize", 3)
        c.ck("job registered open", bt.status().get("open") == 1, str(bt.status()))

        print("\n== in_progress → job stays OPEN (nothing settled, not failed) ==")
        callio.batch_status = lambda ids, **k: {i: {"status": "in_progress", "output_ready": False} for i in ids}
        callio.collect_chat_tasks = lambda bid, intent, model, **k: {"results": {}, "failed": {}, "not_ready": [bid]}
        r1 = bt.poll()
        c.ck("in_progress → open, 0 settled/failed", r1["jobs_open"] == 1 and r1["jobs_settled"] == 0
             and r1["jobs_failed"] == 0 and bt.status().get("open") == 1, str(r1))

        print("\n== completed → rows settle (out-of-order) → job SETTLED ==")
        callio.batch_status = lambda ids, **k: {i: {"status": "completed", "output_ready": True} for i in ids}
        callio.collect_chat_tasks = lambda bid, intent, model, **k: {
            "results": {s_ids[2]: "C", s_ids[0]: "A", s_ids[1]: "B"}, "failed": {}, "not_ready": []}  # OUT OF ORDER
        r2 = bt.poll()
        c.ck("completed → 3 rows settled, job settled", r2["settled_rows"] == 3 and bt.status().get("settled") == 1
             and lq.queue_depth().get("done") == 3, str({"r2": r2, "depth": lq.queue_depth()}))

        print("\n== EXPIRED batch → its rows FAIL OVER to the realtime retry ladder, job FAILED ==")
        [lq.enqueue("chat", "c %d" % i) for i in range(2)]
        chat_ids = _lease_ids("chat")
        lq.mark_batched(chat_ids, "batch-2", "openai:gpt-5-nano")
        bt.register_batch("batch-2", "openai", "openai:gpt-5-nano", "chat", 2, expires_at=1.0)   # already long past
        callio.batch_status = lambda ids, **k: {i: {"status": "expired", "output_ready": False} for i in ids}
        callio.collect_chat_tasks = lambda bid, intent, model, **k: {"results": {}, "failed": {}, "not_ready": []}
        r3 = bt.poll()
        c.ck("expired → job failed", r3["jobs_failed"] == 1 and bt.status().get("failed") == 1, str(r3))
        d3 = lq.queue_depth()
        c.ck("its 2 rows back to realtime PENDING (not stuck queued_batch)",
             d3.get("pending") == 2 and d3.get("queued_batch", 0) == 0, str(d3))

        print("\n== crash-safety: a 'failing' job RESUMES to 'failed' (no double-processing) ==")
        lq.enqueue("resume-intent", "r")
        r_ids = _lease_ids("resume-intent")
        lq.mark_batched(r_ids, "batch-3", "openai:gpt-5-nano")
        bt.register_batch("batch-3", "openai", "openai:gpt-5-nano", "resume-intent", 1)
        bt._set_status("batch-3", "failing", "crash mid-fail")     # simulate a crash BETWEEN requeue and mark-failed
        callio.batch_status = lambda ids, **k: {i: {"status": "expired"} for i in ids}
        r4 = bt.poll()
        c.ck("'failing' job resumed → failed, its row re-queued realtime", r4["jobs_resumed"] >= 1
             and r4["jobs_failed"] >= 1 and lq.queue_depth().get("queued_batch", 0) == 0, str(r4))
    finally:
        callio.batch_status, callio.collect_chat_tasks = real_status, real_collect

    print(f"\n{'[FAIL]' if c.fails else 'OK'} test_batch_tracker: {c.fails} failure(s)")
    return 1 if c.fails else 0


if __name__ == "__main__":
    sys.exit(main())
