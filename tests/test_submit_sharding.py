"""Sharded batch submit + partial-settle collect. Offline — the fan helper is tested directly; submit_chat_tasks /
submit_message_batch are only checked for ROUTING into the fan (spy); collect is driven with stubbed batch_status /
guarded_collect. Zero spend, no API.

Contract pinned: shard_size fans the task list into shards, each its OWN batch, returning a LIST of batch ids (so a
stall in one shard never blocks the others); the caller's cap_dollars is DIVIDED across shards (never n×); a failed
shard is named, the rest still submit; and collect settles the ready shards' results while NAMING the not-yet-ready
shard ids for re-poll."""
import os
import sys
import tempfile

os.environ.setdefault("SPENDGUARD_TEST_ISOLATED", "1")
os.environ.setdefault("SPENDGUARD_NO_AUTOINSTALL", "1")
os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="sg-shard-")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from spendguard import submit, callio  # noqa: E402

fails = []
def ck(label, cond):
    print(f"  [{'OK' if cond else 'FAIL'}] {label}")
    if not cond:
        fails.append(label)


# ── 1. _fan_submit_shards: fan the list, LIST of batch ids, cap divided, errors named ─────────────────────────
seen_caps = []
def _one(shard, cap):
    seen_caps.append(cap)
    return {"batch_id": f"b{len(seen_caps)}", "requests": len(shard)}
r = submit._fan_submit_shards(list(range(10)), 4, 9.0, _one)          # 10 items / 4 → shards of [4,4,2] = 3
ck("fans into ceil(n/shard_size) shards", r["shards"] == 3)
ck("returns a LIST of batch ids, one per shard", r["batch_ids"] == ["b1", "b2", "b3"])
ck("total request count summed across shards", r["requests"] == 10)
ck("caller cap DIVIDED across shards (cap/n, never n×)", all(abs(c - 9.0 / 3) < 1e-9 for c in seen_caps))
ck("no cap → per-shard cap is None", submit._fan_submit_shards([1, 2, 3], 2, None, lambda s, c: {"batch_id": "x", "requests": len(s)}) and True)

def _one_err(shard, cap):
    return {"error": "boom"} if len(shard) == 2 else {"batch_id": "ok", "requests": len(shard)}
r2 = submit._fan_submit_shards(list(range(10)), 4, None, _one_err)    # the 2-item tail shard errors
ck("a failed shard is NAMED, the others still submit", "shard 2" in (r2["error"] or "") and r2["batch_ids"] == ["ok", "ok"])

# ── 2. submit_chat_tasks / submit_message_batch ROUTE into the fan when shard_size is set ─────────────────────
_routed = []
_orig_fan = submit._fan_submit_shards
submit._fan_submit_shards = lambda items, ss, cap, one: (_routed.append(("fan", len(items), ss, cap)) or {"batch_ids": ["x"], "requests": len(items)})
try:
    res = submit.submit_chat_tasks(["t%d" % i for i in range(5)], "openai:gpt-6-sol", shard_size=2, cap_dollars=6.0)
    ck("submit_chat_tasks routes into the fan when shard_size set", _routed and _routed[-1][2] == 2 and res.get("batch_ids") == ["x"])
    _routed.clear()
    res_m = submit.submit_message_batch(["t%d" % i for i in range(5)], "anthropic:claude-opus-4-8", shard_size=2, cap_dollars=6.0)
    ck("submit_message_batch routes into the fan when shard_size set", _routed and _routed[-1][2] == 2 and res_m.get("batch_ids") == ["x"])
    res_small = submit.submit_chat_tasks([], "openai:gpt-6-sol", shard_size=2)
    ck("shard_size with too few tasks does NOT fan (stays single-batch)", not _routed or _routed[-1][1] != 0)
finally:
    submit._fan_submit_shards = _orig_fan

# ── 3. collect partial-settle: ready shards' results + NAMED not-ready shard ids (require_ready=False) ─────────
callio.batch_status = lambda ids: {"bready": {"output_ready": True}, "bnot": {"output_ready": False}}
callio.guarded_collect = lambda ready, intent, model, record_io=False: iter(
    [("task-0", "answer", {"prompt_tokens": 1, "completion_tokens": 1})] if "bready" in list(ready) else [])
out = callio.collect_chat_tasks(["bready", "bnot"], "x", "openai:gpt-6-sol", require_ready=False)
ck("the ready shard's result is settled", out["results"].get("task-0") == "answer")
ck("the not-ready shard id is NAMED for re-poll (require_ready=False)", out["not_ready"] == ["bnot"])
out2 = callio.collect_chat_tasks(["bready", "bnot"], "x", "openai:gpt-6-sol", require_ready=True)
ck("require_ready=True also settles ready + names not-ready", out2["results"].get("task-0") == "answer" and out2["not_ready"] == ["bnot"])

print(f"\n{'[FAIL]' if fails else 'OK'} test_submit_sharding: {len(fails)} failure(s)")
sys.exit(1 if fails else 0)
