"""Offline test for collect_batched's PROVIDER DISPATCH (lane_queue.collect_batched). A queued_batch row carries its
batch's own model in the handle; collect_batched derives the provider from it and settles via the matching collect twin —
an Anthropic Message Batch via callio.collect_message_batch, an OpenAI chat batch via callio.collect_chat_tasks. NO
network, NO spend: the queue is the real (isolated) DB; both collect twins are faked. Sets its OWN fake keys.

Pins:
  · a queued_batch row whose handle model is an Anthropic id → collect_message_batch (NOT collect_chat_tasks);
  · a queued_batch row whose handle model is an OpenAI id → collect_chat_tasks (NOT collect_message_batch);
  · each twin is called with the batch's OWN model (from the handle), not the function's default.
"""
import os
import sys
import tempfile

os.environ["SPENDGUARD_HOME"] = os.environ.get("SPENDGUARD_HOME") or tempfile.mkdtemp(prefix="sg-collect-dispatch-")
os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"
os.environ.setdefault("SPENDGUARD_NO_AUTOINSTALL", "1")
os.environ["ANTHROPIC_API_KEY"] = "sk-ant-test-FAKE"
os.environ["OPENAI_API_KEY"] = "sk-test-FAKE"
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from spendguard import lane_queue, callio   # noqa: E402

ANTHRO = "claude-haiku-4-5"
OPENAI = "gpt-5.5"


def main():
    fails = []

    def ck(name, cond, extra=""):
        print(("  [OK] " if cond else "  [FAIL] ") + name + (("  — " + str(extra)) if extra and not cond else ""))
        if not cond:
            fails.append(name)

    seen = {"msg": [], "chat": []}

    def _fake_msg(batch_id, intent, model, require_ready=True, record_io=False):
        seen["msg"].append((batch_id, model))
        return {"results": {}, "failed": {}, "anomalies": [], "not_ready": [], "collected": 0, "batches": 1}

    def _fake_chat(batch_ids, intent, model, require_ready=True, record_io=False):
        seen["chat"].append((batch_ids, model))
        return {"results": {}, "failed": {}, "anomalies": [], "not_ready": [], "collected": 0, "batches": 1}

    _orig = (callio.collect_message_batch, callio.collect_chat_tasks)
    callio.collect_message_batch = _fake_msg
    callio.collect_chat_tasks = _fake_chat
    try:
        # An Anthropic-model batch and an OpenAI-model batch, both in queued_batch state (real enqueue + real mark_batched)
        rid_a = lane_queue.enqueue("test:dispatch", "task A")
        rid_o = lane_queue.enqueue("test:dispatch", "task B")
        ck("enqueue returned row ids", bool(rid_a) and bool(rid_o), (rid_a, rid_o))
        lane_queue.lease(10)                                                  # pending → leased (as the drain does before offload)
        ma = lane_queue.mark_batched([rid_a], "msgbatch_anthro", ANTHRO)       # handle carries the Anthropic model
        mo = lane_queue.mark_batched([rid_o], "batch_openai", OPENAI)          # handle carries the OpenAI model
        ck("both rows marked queued_batch", ma == 1 and mo == 1, (ma, mo))

        lane_queue.collect_batched()                                          # model defaults to None → per-batch handle model used

        ck("Anthropic-model batch → collect_message_batch called", seen["msg"] == [("msgbatch_anthro", ANTHRO)], seen["msg"])
        ck("OpenAI-model batch → collect_chat_tasks called", seen["chat"] == [("batch_openai", OPENAI)], seen["chat"])
        ck("Anthropic batch NOT sent to the OpenAI twin", all(b != "msgbatch_anthro" for b, _ in seen["chat"]))
        ck("OpenAI batch NOT sent to the Anthropic twin", all(b != "batch_openai" for b, _ in seen["msg"]))
    finally:
        callio.collect_message_batch, callio.collect_chat_tasks = _orig

    print(f"\n{'[FAIL]' if fails else 'OK'} test_collect_batched_dispatch: {len(fails)} failure(s)")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
