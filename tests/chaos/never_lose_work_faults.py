"""Durable-write and drain fault injection over real whole-job and lane-queue accounting."""
import concurrent.futures
import datetime
import os
import pathlib
import runpy
import sys
import tempfile
from unittest.mock import patch

os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="sg-work-chaos-")
os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"

# Let tests/-level siblings loaded via runpy import _script_checks.
_never_lose_work_tests_directory = str(pathlib.Path(__file__).resolve().parents[1])
if _never_lose_work_tests_directory not in sys.path:
    sys.path.insert(0, _never_lose_work_tests_directory)

from spendguard import config, lane_balance, lane_queue, models, whole_job  # noqa: E402
from spendguard.work_integrity import WorkIntegrityProof, WorkIntegrityViolation  # noqa: E402
import spendguard  # noqa: E402

spendguard.require()


def run_never_lose_work_chaos_contract():
    mutation = os.getenv("SPENDGUARD_CHAOS_MUTATION", "intact")
    if mutation == "swallow_persist_failure":
        whole_job._persist_pending = lambda *a: True
    if mutation == "disable_disposition_assertion":
        WorkIntegrityProof.assert_work_partition = lambda *a: None
    if mutation == "rebuy_expired_hold":
        real_settle = lane_queue.settle

        def reopen_expired_hold(rid, result):
            result = {**result, "outcome": None} if result.get("_free_hold") else result
            return real_settle(rid, result)

        lane_queue.settle = reopen_expired_hold
    jobs = [{"id": f"paid-{n}", "prompt": f"prompt-{n}", "intent": "chaos:paid"} for n in range(3)]
    fixture = runpy.run_path(str(pathlib.Path(__file__).parents[1] / "test_batch_offload_exactly_once.py"))
    import openai
    registry = fixture["_Registry"]()
    # Invalid parent directory injects a real durable-write failure, not a replacement receipt.
    checkpoint = os.path.join(tempfile.mkdtemp(), "absent", "checkpoint")
    with patch.object(whole_job, "_method_for", return_value=("batch", 0.0, "fixture")), \
            patch.object(config, "advisor_model", return_value=fixture["MODEL"]), \
            patch.object(models, "resolve_effort", return_value=None), \
            patch.object(openai, "OpenAI", side_effect=lambda *a, **kw: fixture["_FakeOpenAI"](registry)):
        result = whole_job.run_jobs(jobs, {"urgency": "batch"}, checkpoint=checkpoint)
    assert registry.created == 1
    handle = result["pending"][0]
    assert result["pending"] == [handle]
    assert result["receipt"]["persist_failures"] == [handle], result
    assert not whole_job.load_pending(checkpoint + ".pending.jsonl")

    # A short executor response must leave a named gap, never silently disappear.
    with patch.object(whole_job, "_method_for", return_value=("realtime", 0.0, "fixture")), \
            patch.object(whole_job, "_run_realtime_group", return_value={jobs[0]["id"]: {"text": "ok"}}):
        short = whole_job.run_jobs(jobs, {"urgency": "realtime"})
    assert set(short["receipt"]["named_gaps"]) == {j["id"] for j in jobs[1:]}

    intent = "chaos:drain-crash"
    ids = lane_queue.enqueue_many(intent, [f"drain-{n}" for n in range(6)])

    class MidDrainCrash(BaseException):
        pass

    def crash_after_one(tasks, *args, **kwargs):
        def interrupted_results():
            yield {"text": "durable-before-crash"}
            raise MidDrainCrash()
        return interrupted_results()

    with patch.object(lane_balance, "bulk_delegate", side_effect=crash_after_one), \
            patch.object(lane_queue, "_qcfg", side_effect=lambda key, default: 0 if key.startswith("queue_planner") else default):
        try:
            lane_queue.drain(batch=6, idle_sleep=0, idle_rounds=1, max_iters=2)
        except MidDrainCrash:
            pass
        else:
            raise AssertionError("mid-drain crash not injected")
        before = lane_queue.row_results(ids)
        assert sum(v["state"] == "done" for v in before.values()) == 1
        future = lane_queue._utcnow() + datetime.timedelta(seconds=lane_queue.LEASE_S_DEFAULT + 1)
        with patch.object(lane_queue, "_utcnow", return_value=future), \
                patch.object(lane_balance, "bulk_delegate", side_effect=lambda tasks, *a, **kw: [{"text": "resumed"} for t in tasks]):
            resumed = lane_queue.drain(batch=6, idle_sleep=0, idle_rounds=1, max_iters=2)
        assert resumed["done"] == len(ids) - 1, resumed
    terminal = lane_queue.row_results(ids)
    WorkIntegrityProof("chaos-drain").assert_work_partition(
        ids, [[rid for rid, value in terminal.items() if value["state"] == "done"]])
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        holds = list(pool.map(lambda n: lane_queue.hold_free_recovery(
            "chaos:expired", f"hold-{n}", lambda: {"error": "timeout"}, deadline_s=0, poll_s=0), range(4)))
    assert all(h["_free_hold"]["deadline_exhausted"] for h in holds)
    assert lane_queue.queue_depth()["failed"] == len(holds)
    assert lane_queue.lease(20) == [], "expired hold re-bought by later drain"
    with patch.object(lane_queue, "record_close", return_value=None):
        try:
            lane_queue.hold_free_recovery("chaos:failed-close", "failed-close", lambda: {"error": "timeout"},
                                          deadline_s=0, poll_s=0)
        except WorkIntegrityViolation:
            pass
        else:
            raise AssertionError("failed terminal write returned silently")
    assert lane_queue.lease(20) == [], "failed cleanup resurrected an expired hold"
    assert lane_queue.queue_depth()["failed"] == len(holds) + 1
    print("GREEN never-lose-work: named persist failure, missing-result gap, mid-drain recovery, expired holds")


if __name__ == "__main__":
    run_never_lose_work_chaos_contract()
