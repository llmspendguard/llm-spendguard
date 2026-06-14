"""Offline test for the semantic cache — EXACT tier only (no network). Isolated home.

Verifies a true-duplicate prompt is served from cache (the call fn is NOT invoked the second time),
which is the zero-risk cost saver.
"""
import os, sys, tempfile, json

if not os.environ.get("SPENDGUARD_TEST_ISOLATED"):
    os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"
    os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="spendguard-semcache-")
    os.execv(sys.executable, [sys.executable] + sys.argv)

from spendguard import semcache

calls = {"n": 0}


def fn(p):
    calls["n"] += 1
    return "RESULT:" + p


print("-- exact cache (zero-risk, true duplicates) --")
a = semcache.cached_call(fn, "classify aspirin", "gpt-5.5", est_cost=0.01)
b = semcache.cached_call(fn, "classify aspirin", "gpt-5.5", est_cost=0.01)   # same prompt → cache hit
c = semcache.cached_call(fn, "classify metformin", "gpt-5.5", est_cost=0.01)  # different → miss
print(f"  [{'OK' if a == b == 'RESULT:classify aspirin' else 'FAIL'}] identical output served")
print(f"  [{'OK' if calls['n'] == 2 else 'FAIL'}] fn called only twice (2nd identical was cached): n={calls['n']}")
s = semcache.stats()
print(f"  [{'OK' if s['exact'] == 1 and s['miss'] == 2 else 'FAIL'}] stats: {s['exact']} exact / {s['miss']} miss")
print(f"  [{'OK' if abs(s['saved'] - 0.01) < 1e-9 else 'FAIL'}] est saved ${s['saved']:.4f} (one avoided call)")
# different model → not a hit even for same prompt
calls["n"] = 0
semcache.cached_call(fn, "classify aspirin", "gpt-5-nano")
print(f"  [{'OK' if calls['n'] == 1 else 'FAIL'}] cache is per-model (same prompt, new model → miss)")

print("-- batch dedup (within-batch dup + already-cached) --")
semcache.put("classify aspirin", "*", "cached-out")     # pre-cache one prompt (simulate prior run)
lines = [{"custom_id": "a", "body": {"messages": [{"role": "user", "content": "classify aspirin"}]}},   # cache hit
         {"custom_id": "b", "body": {"messages": [{"role": "user", "content": "classify metformin"}]}},  # new
         {"custom_id": "c", "body": {"messages": [{"role": "user", "content": "classify metformin"}]}}]   # within-batch dup
inp = tempfile.mktemp(suffix=".jsonl"); out = tempfile.mktemp(suffix=".jsonl")
open(inp, "w").write("\n".join(json.dumps(x) for x in lines))
r = semcache.dedup_jsonl(inp, out, model="*")
print(f"  [{'OK' if r['kept'] == 1 else 'FAIL'}] only 1 unique-new request kept (metformin once)")
print(f"  [{'OK' if r['cache_hit'] == 1 else 'FAIL'}] 1 already-cached skipped (aspirin)")
print(f"  [{'OK' if r['within_dup'] == 1 else 'FAIL'}] 1 within-batch dup collapsed (2nd metformin)")
print("done.")
