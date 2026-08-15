"""Batch-B semcache correctness closure (line-by-line medium fixes):

  * [85] a legacy db with duplicate (model, prompt_hash) rows MIGRATES (dedupes) instead of bricking on the
    UNIQUE-index build.
  * [86] put() is an atomic upsert — a re-put replaces the output (one row, no IntegrityError) and COALESCE
    keeps a previously-stored embedding when the new put has none.
  * [87] _line_prompt distinguishes the same user message under DIFFERENT system prompts (was a silent
    collision); a bare single-user request still reduces to its raw content (consistency with cached_call).
  * [88] dedup_jsonl passes a valid-JSON-non-object line through instead of crashing on o.get().

(Not changed, by design: [4] a '*' dedup does NOT match a real model's row — that would reuse model-specific
output cross-model; [2] the semantic tier only matches embedded entries.)

Offline, isolated home.
"""
import os
import sys
import tempfile
import json
import sqlite3

if not os.environ.get("SPENDGUARD_TEST_ISOLATED"):
    os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"
    os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="spendguard-medB-")
    os.execv(sys.executable, [sys.executable] + sys.argv)

from spendguard import semcache, config     # noqa: E402

fails = 0


def ck(label, cond, extra=""):
    global fails
    if not cond:
        fails += 1
    print(f"  [{'OK' if cond else 'FAIL'}] {label}{('  — ' + extra) if extra and not cond else ''}")


# ── [85] legacy duplicate rows migrate on index build ───────────────────────────────────────────────────────────
con = sqlite3.connect(config.db_path())
con.execute("CREATE TABLE IF NOT EXISTS semcache(id TEXT PRIMARY KEY, ts TEXT, model TEXT, prompt_hash TEXT, "
            "prompt TEXT, output TEXT, emb BLOB)")
con.execute("INSERT INTO semcache VALUES ('id1','t','m','H','p','OLD',NULL)")
con.execute("INSERT INTO semcache VALUES ('id2','t','m','H','p','NEW',NULL)")   # duplicate (m, H) — no index yet
con.commit()
con.close()
semcache._conn = None                            # reset the singleton so _db() runs the (migrating) init path
migrated, n = True, -1
try:
    n = semcache._db().execute("SELECT COUNT(*) FROM semcache WHERE model='m' AND prompt_hash='H'").fetchone()[0]
except Exception:
    migrated = False
ck("legacy duplicate rows migrate (dedupe) instead of bricking the cache", migrated and n == 1, f"n={n}")

# ── [86] put is an atomic upsert; COALESCE preserves an embedding ────────────────────────────────────────────────
semcache._embed = lambda t: [1.0, 2.0, 3.0]      # deterministic embedding, offline
semcache.put("pp", "mm", "OUT1", store_embedding=True)
semcache.put("pp", "mm", "OUT2", store_embedding=False)   # re-put with NO embedding
ck("re-put replaces the output", semcache.get("pp", "mm") == "OUT2")
with semcache._lock:
    cnt, emb = semcache._db().execute(
        "SELECT COUNT(*), COUNT(emb) FROM semcache WHERE model='mm' AND prompt_hash=?",
        (semcache._hash("pp"),)).fetchone()
ck("exactly one row for the key (atomic upsert, no dup)", cnt == 1, f"cnt={cnt}")
ck("a prior embedding survives a later embedding-less put (COALESCE)", emb == 1, f"emb={emb}")

# ── [87] system prompt distinguishes otherwise-identical user messages ───────────────────────────────────────────
a = {"body": {"messages": [{"role": "user", "content": "hello"}], "system": "You are A"}}
b = {"body": {"messages": [{"role": "user", "content": "hello"}], "system": "You are B"}}
plain = {"body": {"messages": [{"role": "user", "content": "hello"}]}}
ck("same user message, different system → different key", semcache._line_prompt(a) != semcache._line_prompt(b))
ck("a bare single-user request reduces to its raw content (cached_call consistency)",
   semcache._line_prompt(plain) == "hello")

# ── [88] dedup_jsonl tolerates a valid-JSON-non-object line ──────────────────────────────────────────────────────
d = tempfile.mkdtemp()
inp, outp = os.path.join(d, "in.jsonl"), os.path.join(d, "out.jsonl")
with open(inp, "w") as f:
    f.write('"just a string"\n')                  # valid JSON, not an object
    f.write('[1,2,3]\n')                           # valid JSON array
    f.write(json.dumps({"custom_id": "x", "body": {"messages": [{"role": "user", "content": "real"}]}}) + "\n")
crashed = False
try:
    semcache.dedup_jsonl(inp, outp)
except Exception:
    crashed = True
ck("dedup_jsonl passes a valid-JSON-non-object line through instead of crashing", not crashed)

print(f"\n{'[FAIL]' if fails else 'OK'} test_medium_closure_batchB: {fails} failure(s)")
sys.exit(1 if fails else 0)
