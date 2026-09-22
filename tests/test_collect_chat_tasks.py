"""Guard — callio.collect_chat_tasks: the SETTLE twin of submit.submit_chat_tasks that closes the batch loop
(submit → wait → collect). A thin wrapper over batch_status (readiness) + guarded_collect (the streaming full pull),
so a caller gets its submitted results keyed by custom_id without touching the OpenAI SDK. Pins (batch_status +
guarded_collect stubbed — no network):
  · a mixed pull settles into results{cid:text} + failed{cid:error} + anomalies[...] — a per-request failure and a
    custom_id-less anomaly row are SURFACED, never silently dropped; collected counts only succeeded rows;
  · require_ready=True SKIPS a batch whose output file isn't ready (it lands in not_ready), and only ready batches
    reach guarded_collect — the async 24h window is never blocked on;
  · require_ready=False passes every batch through (not_ready empty);
  · record_io flows through to guarded_collect;
  · a string batch_id is normalised to a list; empty ids → a zeroed no-op."""
import os
import sys
import tempfile

os.environ.setdefault("SPENDGUARD_TEST_ISOLATED", "1")
os.environ.setdefault("SPENDGUARD_HOME", tempfile.mkdtemp(prefix="spendguard-collect-"))

from spendguard import callio

_fails = []
def ck(label, cond):
    if not cond:
        _fails.append(label)
    print(f"  [{'OK' if cond else 'FAIL'}] {label}")

# ── stubs: readiness map + a controlled result stream (2 ok, 1 failure, 1 anomaly) ──
callio.batch_status = lambda ids, client=None: {b: {"output_ready": b != "B_NOTREADY"} for b in ids}
_gc_calls = []
def _stub_guarded_collect(ids, intent, model, client=None, record_io=False):
    _gc_calls.append({"ids": list(ids), "record_io": record_io, "intent": intent, "model": model})
    yield ("task-0", "hello", {"completion_tokens": 5})
    yield ("task-1", "world", {"completion_tokens": 6})
    yield ("task-2", None, {"error": "rate limited"})                 # a per-request failure
    yield (None, None, {"error": "batch output row missing custom_id", "raw": "{...}"})  # an anomaly row
callio.guarded_collect = _stub_guarded_collect

print("-- a mixed pull settles into results / failed / anomalies (nothing silently dropped) --")
_gc_calls.clear()
out = callio.collect_chat_tasks(["B1"], "myintent", "gpt-4.1-nano")
ck("succeeded rows keyed by custom_id", out["results"] == {"task-0": "hello", "task-1": "world"})
ck("collected counts only succeeded rows", out["collected"] == 2)
ck("a per-request failure is surfaced by id", out["failed"] == {"task-2": "rate limited"})
ck("a custom_id-less row is surfaced as an anomaly", len(out["anomalies"]) == 1
   and "missing custom_id" in out["anomalies"][0]["error"])
ck("batches counted", out["batches"] == 1)
ck("guarded_collect received the intent + model", _gc_calls[0]["intent"] == "myintent"
   and _gc_calls[0]["model"] == "gpt-4.1-nano")

print("-- require_ready=True: a not-ready batch lands in not_ready; only ready batches reach guarded_collect --")
_gc_calls.clear()
out2 = callio.collect_chat_tasks(["B1", "B_NOTREADY"], "myintent", "gpt-4.1-nano")
ck("the not-ready batch is reported, not collected", out2["not_ready"] == ["B_NOTREADY"])
ck("only the READY batch was streamed", _gc_calls and _gc_calls[0]["ids"] == ["B1"])

print("-- require_ready=False: every batch passes through (not_ready empty) --")
_gc_calls.clear()
out3 = callio.collect_chat_tasks(["B1", "B_NOTREADY"], "myintent", "gpt-4.1-nano", require_ready=False)
ck("no readiness gate → not_ready empty", out3["not_ready"] == [])
ck("all batches streamed", _gc_calls and _gc_calls[0]["ids"] == ["B1", "B_NOTREADY"])

print("-- record_io flows through to guarded_collect --")
_gc_calls.clear()
callio.collect_chat_tasks(["B1"], "myintent", "gpt-4.1-nano", record_io=True)
ck("record_io forwarded", _gc_calls and _gc_calls[0]["record_io"] is True)

print("-- a string batch_id is normalised to a list --")
_gc_calls.clear()
out4 = callio.collect_chat_tasks("B1", "myintent", "gpt-4.1-nano")
ck("string id normalised (one batch)", out4["batches"] == 1 and out4["collected"] == 2)

print("-- empty ids → a zeroed no-op, guarded_collect never called --")
_gc_calls.clear()
out5 = callio.collect_chat_tasks([], "myintent", "gpt-4.1-nano")
ck("empty ids → zeroed no-op, guarded_collect never called",
   out5 == {"results": {}, "failed": {}, "anomalies": [], "not_ready": [], "collected": 0, "batches": 0}
   and not _gc_calls)

print(f"\n{'[FAIL]' if _fails else 'OK'} test_collect_chat_tasks: {len(_fails)} failure(s)")
sys.exit(1 if _fails else 0)
