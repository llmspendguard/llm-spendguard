"""semcache writer/reader scope drift — a verified-high SEAM defect from the 4-LLM review.

put()/cached_call store a row under the CONCRETE model, and the batch tools (populate_jsonl/dedup_jsonl) default
to the any-model sentinel "*". But get() matched only an EXACT model, so a cache POPULATED under the wildcard was
INVISIBLE to a concrete-model read (cached_call) and got re-paid — the writer and reader disagreed on the scope.
get() now matches the given model OR the wildcard (_SCOPE_ANY), the same tolerance dedup_jsonl already used.

Offline, isolated home.
"""
import os
import sys
import tempfile

if not os.environ.get("SPENDGUARD_TEST_ISOLATED"):
    os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"
    os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="spendguard-semcache-")
    os.execv(sys.executable, [sys.executable] + sys.argv)

from spendguard import semcache   # noqa: E402

fails = 0


def ck(label, cond, extra=""):
    global fails
    if not cond:
        fails += 1
    print(f"  [{'OK' if cond else 'FAIL'}] {label}{('  — ' + extra) if extra and not cond else ''}")


# a wildcard-scoped (any-model) cache row must be found by a concrete-model read (the drift the review found)
semcache.put("prompt-A", semcache._SCOPE_ANY, "OUT-any")
ck("a wildcard-scoped cache row is found by a CONCRETE-model read (writer/reader scopes now agree)",
   semcache.get_cached("prompt-A", "gpt-5.5") == "OUT-any")

# a concrete-model row is found by that model...
semcache.put("prompt-B", "m1", "OUT-m1")
ck("a concrete-model row is found by that model", semcache.get_cached("prompt-B", "m1") == "OUT-m1")

# ...but must NOT leak across different concrete models
ck("a concrete-model row does NOT leak to a different concrete model", semcache.get_cached("prompt-B", "m2") is None)

# and a plain miss is still a miss
ck("an uncached prompt is a miss", semcache.get_cached("prompt-never-seen", "m1") is None)

print(f"\n{'[FAIL]' if fails else 'OK'} test_semcache_scope_is_consistent: {fails} failure(s)")
sys.exit(1 if fails else 0)
