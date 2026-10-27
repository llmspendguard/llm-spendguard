"""Fake provider transport; real submit, reconciliation, registry, queue and collect."""
import concurrent.futures
import collections
import json
import os
import pathlib
import runpy
import sys
import tempfile
from unittest.mock import patch

os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="sg-exactly-chaos-")
os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"

# Let tests/-level siblings loaded via runpy import _script_checks.
_exactly_once_tests_directory = str(pathlib.Path(__file__).resolve().parents[1])
if _exactly_once_tests_directory not in sys.path:
    sys.path.insert(0, _exactly_once_tests_directory)

from spendguard import batch_tracker, callio, lane_queue, models, submit  # noqa: E402
from spendguard.work_integrity import WorkIntegrityProof, WorkIntegrityViolation  # noqa: E402
import spendguard  # noqa: E402

spendguard.require()

EXACTLY_ONCE_ROW_COUNT = 6


def run_exactly_once_chaos_contract():
    # Reuse the existing fake SDK rather than invent a second provider simulation.
    fixture = runpy.run_path(str(pathlib.Path(__file__).parents[1] / "test_batch_offload_exactly_once.py"))
    import openai
    registry = fixture["_Registry"]()
    submitted_ids = []

    def capture_batch_upload(*, file, purpose, **kwargs):
        submitted_ids.extend(json.loads(line)["custom_id"] for line in file if line.strip())
        return fixture["_FakeFile"]("file_test_1")
    mutation = os.getenv("SPENDGUARD_CHAOS_MUTATION", "intact")
    if mutation == "disable_idempotency":
        batch_tracker._existing_offload_batch = lambda *a: None
    if mutation == "drop_persisted_handle":
        lane_queue.mark_batched = lambda *a: 0
        batch_tracker.register_batch = lambda *a, **kw: None
    if mutation == "disable_message_idempotency":
        batch_tracker._existing_offload_message_batch = lambda *a: None
    if mutation == "drop_message_pending_record":
        batch_tracker._pending_note = lambda *a: None
    if mutation == "disable_terminal_manifest_assertion":
        batch_tracker.assert_batch_terminal_manifest = lambda *a: None
    if mutation == "disable_global_ordinals":
        submit._assign_batch_ordinals = lambda tasks: list(tasks)
    model, intent = fixture["MODEL"], "chaos:exactly-once"
    enqueued_ids = lane_queue.enqueue_many(intent, [f"ordinal-{n}" for n in range(EXACTLY_ONCE_ROW_COUNT)])
    rows = lane_queue.lease(EXACTLY_ONCE_ROW_COUNT)
    if len(enqueued_ids) != EXACTLY_ONCE_ROW_COUNT or len(rows) != EXACTLY_ONCE_ROW_COUNT:
        raise AssertionError(
            f"exactly-once setup lost queued work: enqueued={enqueued_ids!r}, leased={rows!r}"
        )
    ids = [r["id"] for r in rows]
    real_mark = lane_queue.mark_batched

    class SubmitPersistCrash(BaseException):
        pass

    def provider_results(*args, **kwargs):
        for rid in reversed(ids):
            yield str(rid), f"result-{rid}", {}

    with patch.object(openai, "OpenAI", side_effect=lambda *a, **kw: fixture["_FakeOpenAI"](registry)), \
            patch.object(fixture["_FakeFiles"], "create", side_effect=capture_batch_upload), \
            patch.object(models, "resolve_effort", return_value=None):
        with patch.object(lane_queue, "mark_batched", side_effect=SubmitPersistCrash):
            try:
                submit_result = batch_tracker.submit_offload(intent, rows, model)
            except SubmitPersistCrash:
                pass
            else:
                raise AssertionError(
                    f"fault did not reach the submit/persist window: submit_offload returned {submit_result!r}"
                )
        assert registry.created == 1
        recovered = batch_tracker.submit_offload(intent, rows, model)
        assert recovered.get("adopted"), recovered
        assert recovered["marked"] == len(ids), recovered
        assert all(v["state"] == "queued_batch" for v in lane_queue.row_results(ids).values())
        # Duplicate callers are serialized by the real queue lease owner, just as the drainer is.
        for _ in range(2):
            batch_tracker.submit_offload(intent, rows, model)
        assert registry.created == 1, "duplicate paid create"
        assert collections.Counter(submitted_ids) == collections.Counter(map(str, ids)), submitted_ids
        assert real_mark is lane_queue.mark_batched
        with patch.object(callio, "batch_status", return_value={recovered["batch_id"]: {"output_ready": True}}), \
                patch.object(callio, "guarded_collect", side_effect=provider_results):
            with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
                collection_runs = list(pool.map(lambda _: lane_queue.collect_batched(), range(4)))
            assert sum(c["collected"] for c in collection_runs) == len(ids), collection_runs
            assert lane_queue.collect_batched()["collected"] == 0
        terminal = lane_queue.row_results(ids)
        WorkIntegrityProof("chaos-exactly-once").assert_work_partition(
            ids, [[rid for rid, value in terminal.items() if value["state"] == "done"]])
        assert all(value["result"]["text"] == f"result-{rid}" for rid, value in terminal.items())
        batch_tracker.assert_batch_terminal_manifest(recovered["batch_id"])
        # Removing a durable terminal result must fail at the real tracker commit assertion.
        with lane_queue._queue_op() as connection:
            connection.execute("DELETE FROM lane_queue WHERE id=?", (ids[0],))
            connection.commit()
        try:
            batch_tracker.assert_batch_terminal_manifest(recovered["batch_id"])
        except WorkIntegrityViolation:
            pass
        else:
            raise AssertionError("tracker accepted a missing terminal result")
        before_shards = len(submitted_ids)
        sharded = submit.submit_chat_tasks([f"shard-{n}" for n in range(6)], model, shard_size=2)
        assert len(sharded["batch_ids"]) == 3, sharded
        assert submitted_ids[before_shards:] == [f"task-{n}" for n in range(6)], submitted_ids
        try:
            submit.submit_chat_tasks([{"custom_id": "duplicate", "content": "a"},
                                      {"custom_id": "duplicate", "content": "b"}], model)
        except WorkIntegrityViolation:
            pass
        else:
            raise AssertionError("ambiguous custom ids accepted")
    proof = WorkIntegrityProof("negative-control")
    try:
        proof.assert_work_partition(ids, [ids, ids])
    except WorkIntegrityViolation:
        assert proof.events[-1]["rung"] == "bug"
    else:
        raise AssertionError("duplicate outcome accepted")
    run_message_exactly_once_chaos_contract()
    print("GREEN exactly-once: submit/persist crash adoption, duplicate submit, concurrent collect and replay")


def run_message_exactly_once_chaos_contract():
    """Exercise Anthropic's distinct pending-before-create and orphan-adoption rungs."""
    import anthropic
    fixture = runpy.run_path(str(pathlib.Path(__file__).parents[1] / "_fake_anthropic.py"))
    model, intent = "claude-haiku-4-5", "chaos:message-exactly-once"
    os.environ["ANTHROPIC_API_KEY"] = "sk-ant-chaos-not-used"
    ids = lane_queue.enqueue_many(intent, [f"message-{n}" for n in range(4)])
    rows = lane_queue.lease(len(ids))
    batch = fixture["batch_obj"]("msgbatch_chaos", "ended", len(ids))
    transport = fixture["FakeBatches"](
        batches={batch.id: batch}, list_return=[batch], create_result=batch,
        results={batch.id: [fixture["succeeded_text"](str(rid), f"message-result-{rid}") for rid in reversed(ids)]})
    client = fixture["FakeAnthropic"](transport)

    class MessagePersistCrash(BaseException):
        pass

    with patch.object(anthropic, "Anthropic", return_value=client), \
            patch.object(callio, "_anthropic_client", return_value=client), \
            patch.object(models, "resolve_effort", return_value=None):
        with patch.object(batch_tracker, "_pending_confirm", side_effect=MessagePersistCrash):
            try:
                batch_tracker.submit_offload(intent, rows, model)
            except MessagePersistCrash:
                pass
            else:
                raise AssertionError("message submit/persist crash not injected")
        recovered = batch_tracker.submit_offload(intent, rows, model)
        assert recovered.get("adopted") and recovered["marked"] == len(ids), recovered
        batch_tracker.submit_offload(intent, rows, model)
        assert len(transport.create_calls) == 1, "message batch double-billed"
        assert collections.Counter(r["custom_id"] for r in transport.create_calls[0]) == collections.Counter(map(str, ids))
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            collected = list(pool.map(lambda _: lane_queue.collect_batched(), range(4)))
        assert sum(c["collected"] for c in collected) == len(ids), collected
        assert lane_queue.collect_batched()["collected"] == 0
        batch_tracker.assert_batch_terminal_manifest(batch.id)
        assert all(v["state"] == "done" for v in lane_queue.row_results(ids).values())
    print("GREEN message exactly-once: durable pending record, orphan adoption, concurrent collect and replay")


if __name__ == "__main__":
    run_exactly_once_chaos_contract()
