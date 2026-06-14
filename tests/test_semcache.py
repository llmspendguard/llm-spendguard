"""Offline test for the semantic cache — EXACT tier only (no network). Isolated home.

Verifies a true-duplicate prompt is served from cache (the call fn is NOT invoked the second time),
which is the zero-risk cost saver.
"""
import os, sys, tempfile

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
print("done.")
