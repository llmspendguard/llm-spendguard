"""Guard (precommit F1, 2026-09-28): collect_batched must count done/failed/collected ONLY when settle() actually
recorded the row. settle() is best-effort — a transient SQLite error returns FALSY without updating the row. Counting
it anyway would report the row COLLECTED while it stays queued_batch, so the count diverges from the DB (a success
that never happened). This proves the count follows the real settle outcome, and that an un-settled row is NAMED (it
stays queued_batch → re-collected next round, never lost, never over-reported).

Offline + deterministic ($0): fakes callio.collect_chat_tasks; no provider call. Isolation: SPENDGUARD_HOME → mkdtemp.
"""
import os, sys, tempfile

os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="spendguard-collect-consistency-")

from spendguard import lane_queue as lq, callio   # noqa: E402


def main():
    fails = 0

    def ck(name, cond, extra=""):
        nonlocal fails
        if not cond:
            fails += 1
        print(f"  [{'OK' if cond else 'FAIL'}] {name}{('  — ' + str(extra)) if extra and not cond else ''}")

    _real_collect, _real_settle = callio.collect_chat_tasks, lq.settle
    try:
        # a queued_batch row whose batch result is ready
        lq.enqueue("ci-intent", "a task")
        ids = [r["id"] for r in lq.lease(10) if r["intent"] == "ci-intent"]
        lq.mark_batched(ids, "batch-ci", "openai:gpt-5-nano")
        callio.collect_chat_tasks = lambda bid, intent, model, **k: {
            "results": {i: "the answer" for i in ids}, "failed": {}, "not_ready": []}

        # 1. settle SILENTLY FAILS (the best-effort transient path returns False) → row NOT counted, and NAMED
        lq.settle = lambda rid, res: False
        out = lq.collect_batched()
        ck("a failed settle is NOT counted as done/collected", out.get("done") == 0 and out.get("collected") == 0, out)
        ck("the un-settled row is NAMED in collect_errors",
           any("settle failed" in str(e) for e in out.get("collect_errors", [])), out)

        # 2. with the REAL settle, the same still-queued_batch row IS counted done/collected
        lq.settle = _real_settle
        out2 = lq.collect_batched()
        ck("a successful settle IS counted done/collected", out2.get("done") >= 1 and out2.get("collected") >= 1, out2)

        # 3. PARTIAL PULL: a batch returns SOME rows and omits others — the omitted row is COUNTED (not_settled), stays
        #    queued_batch, and is never silently skipped (each row is a unit of work).
        lq.enqueue("partial-intent", "task A")
        lq.enqueue("partial-intent", "task B")
        pids = [r["id"] for r in lq.lease(10) if r["intent"] == "partial-intent"]
        lq.mark_batched(pids, "batch-partial", "openai:gpt-5-nano")
        callio.collect_chat_tasks = lambda bid, intent, model, **k: {
            "results": {pids[0]: "A done"}, "failed": {}, "not_ready": []}   # pids[1] OMITTED from both
        out3 = lq.collect_batched()
        ck("a partial pull COUNTS the omitted row in not_settled (never silently skipped)",
           out3.get("not_settled", 0) >= 1 and out3.get("done") >= 1, out3)
    finally:
        callio.collect_chat_tasks, lq.settle = _real_collect, _real_settle

    print(f"\n{'[FAIL]' if fails else '[OK]'} test_collect_batched_settle_consistency: {fails} failure(s)")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
