"""Provider batch resilience guard: offline; upload and create are mocked."""
import json
import os
import sys
import tempfile
import types

os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"
os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="sg-provider-resilience-")
os.environ["SPENDGUARD_BULK_RESILIENCE_MIN_UNITS"] = "3"
os.environ["OPENAI_API_KEY"] = "fake-offline-key"
os.environ["ANTHROPIC_API_KEY"] = "fake-offline-key"

from spendguard import submit, batch_tracker, lane_queue, gate, bulk_resilience, lane_balance

failures = []


def check(label, condition):
    if not condition:
        failures.append(label)
    print(f"  [{'OK' if condition else 'FAIL'}] {label}")


created = {"openai": [], "anthropic": [], "uploads": 0}


class FakeOpenAI:
    def __init__(self, **kwargs):
        self.files = types.SimpleNamespace(create=self.upload)
        self.batches = types.SimpleNamespace(create=self.create)

    def upload(self, **kwargs):
        created["uploads"] += 1
        return types.SimpleNamespace(id=f"file-{created['uploads']}")

    def create(self, **kwargs):
        created["openai"].append(kwargs)
        return types.SimpleNamespace(id=f"openai-{len(created['openai'])}")


class FakeAnthropic:
    def __init__(self, **kwargs):
        self.messages = types.SimpleNamespace(batches=types.SimpleNamespace(create=self.create))

    def create(self, **kwargs):
        created["anthropic"].append(kwargs)
        return types.SimpleNamespace(id=f"anthropic-{len(created['anthropic'])}")


sys.modules["openai"] = types.SimpleNamespace(OpenAI=FakeOpenAI)
sys.modules["anthropic"] = types.SimpleNamespace(Anthropic=FakeAnthropic)
submit._api_key = lambda name: "fake-offline-key"
submit._preflight_first_request = lambda *args: {"ok": True}
gate.record_accepted_batch_estimate = lambda *args, **kwargs: None
gate._cap = lambda: 1000.0
gate.estimate_message_batch = lambda requests, **kwargs: {
    "requests": len(requests), "in_tok": 1, "out_tok": 1, "cost": 0.0,
    "out_basis": "test", "model": "anthropic:claude-haiku-4-5"}
submit.build_message_batch_requests = lambda tasks, model, **kwargs: (
    [{"custom_id": str(i), "params": {"model": model, "messages": []}} for i, _ in enumerate(tasks)], len(tasks))


def fake_build_chat(tasks_path, model, **kwargs):
    rows = [json.loads(line) for line in open(tasks_path)]
    fd, path = tempfile.mkstemp(suffix=".jsonl")
    with os.fdopen(fd, "w") as handle:
        for row in rows:
            handle.write(json.dumps({"custom_id": row["custom_id"], "body": {"model": model,
                         "messages": [{"role": "user", "content": row["content"]}]}}) + "\n")
    return path, len(rows)


submit.build_chat_batch_jsonl = fake_build_chat
submit.estimate_jsonl_cost = lambda path, model, **kwargs: {
    "requests": sum(1 for _ in open(path)), "mode": "batch", "in_tok": 1, "out_tok": 1,
    "out_basis": "test", "token_basis": "test", "cost": 0.0, "model": model}

threshold = bulk_resilience._resilience_min_units()
large = ["request" for _ in range(threshold)]


def refused(call, where, *, chunked=False):
    before = (len(created["openai"]), len(created["anthropic"]), created["uploads"])
    try:
        call()
    except bulk_resilience.BulkResilienceRefused as exc:
        check(f"{where}: typed refusal with request count and threshold",
              exc.n_units == threshold and exc.threshold == threshold and exc.where == where
              and exc.chunked is chunked and exc.checkpointed is False)
    else:
        check(f"{where}: typed refusal", False)
    check(f"{where}: no upload or create", before == (
        len(created["openai"]), len(created["anthropic"]), created["uploads"]))


refused(lambda: submit.submit_chat_tasks(large, "openai:gpt-5-nano", preflight=False), "submit_chat_tasks")
refused(lambda: submit.submit_message_batch(large, "anthropic:claude-haiku-4-5", preflight=False),
        "submit_message_batch")
refused(lambda: submit.submit_chat_tasks(large, "openai:gpt-5-nano", shard_size=threshold - 1),
        "submit_chat_tasks", chunked=True)
refused(lambda: submit.submit_message_batch(large, "anthropic:claude-haiku-4-5",
                                            shard_size=threshold - 1),
        "submit_message_batch", chunked=True)
refused(lambda: submit.submit_chat_tasks(large, "openai:gpt-5-nano", shard_size=threshold),
        "submit_chat_tasks")
refused(lambda: submit.submit_message_batch(large, "anthropic:claude-haiku-4-5", shard_size=threshold),
        "submit_message_batch")

fd, raw = tempfile.mkstemp(suffix=".jsonl")
with os.fdopen(fd, "w") as handle:
    for _ in large:
        handle.write('{}\n')
refused(lambda: submit.guarded_submit(raw, "openai:gpt-5-nano", None, preflight=False), "guarded_submit")

small = large[:-1]
small_chat = submit.submit_chat_tasks(small, "openai:gpt-5-nano", preflight=False)
small_message = submit.submit_message_batch(small, "anthropic:claude-haiku-4-5", preflight=False)
check("small batches reach mocked create", bool(small_chat["batch_id"] and small_message["batch_id"]))

from contextlib import redirect_stderr
from io import StringIO
log = StringIO()
with redirect_stderr(log):
    forced_chat = submit.submit_chat_tasks(large, "openai:gpt-5-nano", preflight=False, force=True)
    forced_message = submit.submit_message_batch(large, "anthropic:claude-haiku-4-5",
                                                  preflight=False, force=True)
check("force permits both provider creates", bool(forced_chat["batch_id"] and forced_message["batch_id"]))
check("force override logged for both doors", log.getvalue().count("force=True override") >= 2)

# The tracker is the checkpointed route: each real shard submit reaches mocked create,
# and each returned handle is marked against its own row ids before the next shard.
marks = []
batch_tracker._existing_offload_batch = lambda *args: None
batch_tracker._existing_offload_message_batch = lambda *args: None
batch_tracker._pending_note = lambda *args: None
batch_tracker._pending_confirm = lambda *args: None
batch_tracker.register_batch = lambda *args, **kwargs: None
lane_queue.mark_batched = lambda ids, bid, model: (marks.append((tuple(ids), bid)) or len(ids))
rows = [{"id": f"row-{i}", "task": "request"} for i in range(threshold)]
anthropic_before_tracker = len(created["anthropic"])
tracker_chat = batch_tracker.submit_offload("offline:test", rows, "openai:gpt-5-nano",
                                            shard_size=threshold - 1)
tracker_message = batch_tracker.submit_offload("offline:test", rows, "anthropic:claude-haiku-4-5",
                                               shard_size=threshold - 1)
check("checkpointed path shards and durably marks every OpenAI row",
      tracker_chat["shards"] > 1 and tracker_chat["marked"] == threshold and len(tracker_chat["batch_ids"]) > 1)
check("checkpointed path shards and durably marks every Anthropic row",
      tracker_message["shards"] > 1 and tracker_message["marked"] == threshold and len(tracker_message["batch_ids"]) > 1)
check("each provider create contains fewer than threshold requests",
      all(len(c["requests"]) < threshold for c in created["anthropic"][anthropic_before_tracker:]))
check("tracker marked each shard with its returned handle", len(marks) >= 4 and
      all(ids and bid for ids, bid in marks))
check("lane exception identity preserved after extraction",
      lane_balance.BulkResilienceRefused is bulk_resilience.BulkResilienceRefused)

print(f"\n{'[FAIL]' if failures else 'OK'} test_provider_batch_resilience_gate: {len(failures)} failure(s)")
sys.exit(1 if failures else 0)
