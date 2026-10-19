"""P0 chaos guard: oversized audit evidence cannot destroy durable control or answer text."""
import json
import os
import sqlite3

from spendguard import lane_queue as queue


PAYLOAD_MULTIPLIER = 8
TEST_INTENT = "queue-result-durability"
TEST_MODEL = "durability-model"
TEST_LANE = "durability-lane"
BROKEN_AUDIT = '{"text":'
OVERSIZED_TEXT = "answer" * (queue._RESULT_CAP * PAYLOAD_MULTIPLIER)
OVERSIZED_HANDLE = "handle" * (queue._RESULT_CAP * PAYLOAD_MULTIPLIER)
MUTATION_ENV = "SPENDGUARD_TEST_MUTATE_QUEUE_RESULT_SLICE"
if os.environ.get(MUTATION_ENV):
    queue._serialize_queue_audit = lambda result: json.dumps(result)[:queue._RESULT_CAP]

absent = queue._enqueue_leased(TEST_INTENT, ["absent-task"])[0]
assert queue.row_results([absent])[absent]["result"] == {}


def assert_queue_durable_audit(row_id):
    with queue._queue_op() as conn:
        payload = conn.execute("SELECT result FROM lane_queue WHERE id=?", (row_id,)).fetchone()[0]
    parsed = json.loads(payload)
    assert parsed["_truncated"] is True
    assert parsed["_orig_bytes"] > queue._RESULT_CAP
    return parsed


# Independent identical rows exercise both settle and its pending-twin coalesce writer.
primary, twin = queue.enqueue_many(TEST_INTENT, ["same-task", "same-task"], dedup=False)
assert queue.lease(1)[0]["id"] == primary
assert queue.settle(primary, {"text": OVERSIZED_TEXT, "lane": TEST_LANE,
                              "metadata": {"complete": OVERSIZED_TEXT}})
for row_id in (primary, twin):
    assert_queue_durable_audit(row_id)
    result = queue.row_results([row_id])[row_id]["result"]
    assert result["_truncated"] is True
    assert result["text"] == OVERSIZED_TEXT
    assert result["metadata"]["complete"] == OVERSIZED_TEXT

# A handle beyond the audit cap must remain discoverable even after independent audit corruption.
queued = queue._enqueue_leased(TEST_INTENT, ["batch-task"])[0]
assert queue.settle(queued, {"reason": "queued_batch", "batch": OVERSIZED_HANDLE,
                             "batch_model": TEST_MODEL, "lane": TEST_LANE})
assert_queue_durable_audit(queued)
assert [row["id"] for row in queue.batched_rows(OVERSIZED_HANDLE)] == [queued]
with queue._queue_op() as conn:
    conn.execute("UPDATE lane_queue SET result=? WHERE id=?", (BROKEN_AUDIT, queued))
before = queue._queue_result_gap_count()
gap = queue.row_results([queued])[queued]["result"]
assert gap["_gap"] == queue._QUEUE_RESULT_GAP
assert queue._queue_result_gap_count() == before
assert before >= 1
assert [row["id"] for row in queue.batched_rows(OVERSIZED_HANDLE)] == [queued]

from spendguard import adapters, callio

adapters.provider_for = lambda model: "openai"
callio.collect_chat_tasks = lambda handle, intent, model: {
    "results": {str(queued): OVERSIZED_TEXT}, "failed": {}, "not_ready": []}
collected = queue.collect_batched()
assert collected["collected"] == 1
assert collected["orphaned"] == 0
assert queue.row_results([queued])[queued]["result"]["text"] == OVERSIZED_TEXT

marked = queue._enqueue_leased(TEST_INTENT, ["marked-task"])[0]
assert queue.mark_batched([marked], OVERSIZED_HANDLE, TEST_MODEL) == 1
assert [row["id"] for row in queue.batched_rows(OVERSIZED_HANDLE)] == [marked]
assert queue.requeue_from_batch([marked], OVERSIZED_TEXT) == 1
requeued_result = queue.row_results([marked])[marked]["result"]
assert requeued_result["error"] == OVERSIZED_TEXT
assert "batch" not in requeued_result
assert_queue_durable_audit(marked)

deadline_row = queue._enqueue_leased(TEST_INTENT, ["deadline-task"])[0]
with queue._queue_op() as conn:
    conn.execute("UPDATE lane_queue SET result_payload=?,deadline_ts=? WHERE id=?",
                 (json.dumps({"text": OVERSIZED_TEXT}), queue._iso(queue._utcnow()), deadline_row))
queue.lease(1)
deadline_result = queue.row_results([deadline_row])[deadline_row]["result"]
assert deadline_result["outcome"] == "deadline_exceeded"
assert "text" not in deadline_result

from spendguard import batch_tracker

batch_tracker.register_batch(OVERSIZED_HANDLE, "openai", TEST_MODEL, TEST_INTENT, 1)
orphan = queue._enqueue_leased(TEST_INTENT, ["corrupt-orphan-task"])[0]
with queue._queue_op() as conn:
    conn.execute("UPDATE lane_queue SET state='queued_batch',result=? WHERE id=?", (BROKEN_AUDIT, orphan))
orphan_collection = queue.collect_batched()
assert orphan_collection["orphaned"] == 1
assert orphan_collection["result_gaps"] >= 1
assert queue.row_results([orphan])[orphan]["state"] == "queued_batch"
tracker_gap = batch_tracker.poll()
assert tracker_gap["jobs_open"] == 1
assert tracker_gap["jobs_settled"] == 0
assert tracker_gap["errors"][0]["row_id"] == orphan
assert tracker_gap["errors"][0]["result"]["_gap"] == queue._QUEUE_RESULT_GAP
assert queue.row_results([orphan])[orphan]["state"] == "queued_batch"
try:
    queue.batched_rows(OVERSIZED_HANDLE)
except queue.QueueBatchHandleGap as missing_handle:
    assert missing_handle.row_id == orphan
    assert missing_handle.evidence["_gap"] == queue._QUEUE_RESULT_GAP
else:
    raise AssertionError("missing batch attribution was silently reported as an empty result")

# Simulate pre-migration legacy evidence and repeat the migration to prove idempotence.
legacy = queue._enqueue_leased(TEST_INTENT, ["legacy-task"])[0]
valid_legacy = queue._enqueue_leased(TEST_INTENT, ["valid-legacy-task"])[0]
with queue._queue_op() as conn:
    conn.execute("UPDATE lane_queue SET result=?,result_migrated=0 WHERE id=?", (BROKEN_AUDIT, legacy))
    conn.execute("UPDATE lane_queue SET result=?,result_migrated=0 WHERE id=?",
                 (json.dumps({"batch": OVERSIZED_HANDLE, "batch_model": TEST_MODEL,
                              "text": OVERSIZED_TEXT, "lane": TEST_LANE}), valid_legacy))
    queue._ensure_queue_schema(conn)
    queue._ensure_queue_schema(conn)
    assert conn.execute("SELECT result_gap,result_migrated FROM lane_queue WHERE id=?",
                        (legacy,)).fetchone() == (queue._QUEUE_RESULT_GAP, 1)
    migrated = conn.execute("SELECT batch_handle,batch_model,result_payload,lane FROM lane_queue WHERE id=?",
                            (valid_legacy,)).fetchone()
    assert migrated[:2] == (OVERSIZED_HANDLE, TEST_MODEL)
    assert json.loads(migrated[2])["text"] == OVERSIZED_TEXT
    assert migrated[3] == TEST_LANE
assert queue.row_results([legacy])[legacy]["result"]["_gap"] == queue._QUEUE_RESULT_GAP

# Exercise ALTER/backfill against the actual original schema, not just a reset migration flag.
with sqlite3.connect(":memory:") as legacy_db:
    legacy_db.execute("""CREATE TABLE lane_queue(
        id INTEGER PRIMARY KEY AUTOINCREMENT, intent TEXT NOT NULL, task TEXT NOT NULL,
        system TEXT, reasoning TEXT, priority INTEGER DEFAULT 0, state TEXT DEFAULT 'pending',
        lease_until TEXT, attempts INTEGER DEFAULT 0, max_attempts INTEGER DEFAULT 3,
        worker TEXT, result TEXT, lane TEXT, billed INTEGER DEFAULT 0, created_ts TEXT, updated_ts TEXT)""")
    legacy_db.execute("INSERT INTO lane_queue(intent,task,result) VALUES(?,?,?)",
                      (TEST_INTENT, TEST_INTENT, BROKEN_AUDIT))
    queue._ensure_queue_schema(legacy_db)
    queue._ensure_queue_schema(legacy_db)
    assert legacy_db.execute("SELECT result_gap,result_migrated FROM lane_queue").fetchone() == (
        queue._QUEUE_RESULT_GAP, 1)

# A future serializer regression must fail at the write boundary, outside best-effort DB handling.
assertion_row = queue._enqueue_leased(TEST_INTENT, ["invalid-serializer-task"])[0]
original_serializer = queue._serialize_queue_audit
try:
    queue._serialize_queue_audit = lambda result: BROKEN_AUDIT
    try:
        queue.settle(assertion_row, {"text": OVERSIZED_TEXT})
    except (AssertionError, ValueError):
        pass
    else:
        raise AssertionError("invalid serialized JSON was silently accepted or swallowed")
finally:
    queue._serialize_queue_audit = original_serializer
print("ok — oversized result durability, coalesce, audit corruption, legacy migration")
