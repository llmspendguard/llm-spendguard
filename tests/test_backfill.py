"""Offline test for backfill — ingest REAL batch ledgers into calls + the learning graph (no spend).

The two provider readers (_openai_rows / _anthropic_rows) are the ONLY network paths; we stub them
with canned rows, so nothing touches OpenAI/Anthropic. Asserts rows land in `calls`, run-nodes land
in the graph, ingestion is idempotent, intents tag rows, and load_intent_map parses both formats.
"""
import os, sys, tempfile

if not os.environ.get("SPENDGUARD_TEST_ISOLATED"):
    os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"
    os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="spendguard-backfill-")
    os.execv(sys.executable, [sys.executable] + sys.argv)

import json
from spendguard import backfill, calls, learn

failures = 0


def check(label, cond):
    global failures
    ok = bool(cond)
    if not ok:
        failures += 1
    print(f"  [{'OK' if ok else 'FAIL'}] {label}")


print("-- _openai_rows: real row-builder, only the INNER network primitives stubbed (no network) --")
import spendguard.reconcile_openai as ro
ro.load_key = lambda: "sk-test"
ro.fetch_batches = lambda k: [
    # completed with usage → a row; cost from pricing (1M batch in = $2.50)
    {"id": "ro-1", "status": "completed", "model": "gpt-5.5-2026-04-23",
     "usage": {"input_tokens": 1_000_000, "output_tokens": 0,
               "input_tokens_details": {"cached_tokens": 0}}, "created_at": 1700000000},
    # in_progress → skipped (status filter)
    {"id": "ro-2", "status": "in_progress", "model": "gpt-5.5", "usage": {}, "created_at": 1700000000},
    # completed but zero usage → skipped (no tokens)
    {"id": "ro-3", "status": "completed", "model": "gpt-5.5", "usage": {}, "created_at": 1700000000},
    # cancelled WITH usage → still counts (completed requests bill)
    {"id": "ro-4", "status": "cancelled", "model": "gpt-5.5",
     "usage": {"input_tokens": 0, "output_tokens": 1_000_000}, "created_at": 1700000000},
]
rows = backfill._openai_rows()
ids = [r[6] for r in rows]
check("_openai_rows keeps completed+cancelled-with-usage only", sorted(ids) == ["ro-1", "ro-4"])
check("_openai_rows normalizes the dated model", rows[0][1] == "gpt-5.5")
check("_openai_rows prices ro-1 at $2.50 (1M batch in)", abs(rows[0][2] - 2.5) < 1e-9)
check("_openai_rows day from created_at (UTC)", rows[0][5] == "2023-11-14")

print("-- _anthropic_rows: real row-builder over a stubbed local cache (no network) --")
import spendguard.reconcile_anthropic as ra
ra.cost_by_day = lambda since=None: ({}, {})        # don't refresh from network
cache_dir = tempfile.mkdtemp()
ra.CACHE_PATH = os.path.join(cache_dir, "anth_cache.json")
json.dump({
    "ab-1": {"created_at": "2026-06-01",
             "by_model": {"claude-opus-4-8": {"in": 1_000_000, "out": 0}}},
    "ab-2": {"created_at": "2026-06-02",
             "by_model": {"made-up-model": {"in": 100, "out": 100}}},   # unknown model → cost 0.0 (except path)
}, open(ra.CACHE_PATH, "w"))
arows = backfill._anthropic_rows()
amap = {r[6]: r for r in arows}
check("_anthropic_rows builds a row per (batch, model)", set(amap) == {"ab-1", "ab-2"})
check("_anthropic_rows prices opus 1M batch in = $2.50", abs(amap["ab-1"][2] - 2.5) < 1e-9)
check("_anthropic_rows unknown model → cost 0.0 (never crashes)", amap["ab-2"][2] == 0.0)
check("_anthropic_rows carries created_at", amap["ab-1"][5] == "2026-06-01")

print("-- _anthropic_rows: empty when no cache file --")
ra.CACHE_PATH = os.path.join(cache_dir, "does-not-exist.json")
check("no cache → no rows", backfill._anthropic_rows() == [])

# from here on, replace the readers with simple canned stubs for the higher-level backfill() tests
# canned provider rows: (provider, model, cost, in_tok, out_tok, ts, batch_id)
OAI = [
    ("openai", "gpt-5.5", 12.50, 5_000_000, 100_000, "2026-06-01", "oai-batch-1"),
    ("openai", "gpt-5.5", 3.00, 1_000_000, 20_000, "2026-06-02", "oai-batch-2"),
]
ANTH = [
    ("anthropic", "claude-opus-4-8", 7.25, 2_000_000, 50_000, "2026-06-01", "anth-batch-1"),
]
backfill._openai_rows = lambda: list(OAI)          # NO network
backfill._anthropic_rows = lambda: list(ANTH)      # NO network


def _calls_count():
    return calls._db().execute("SELECT COUNT(*) FROM calls WHERE caller='backfill:ledger'").fetchone()[0]


def _run_nodes():
    return {r[0] for r in learn._db().execute("SELECT id FROM graph_nodes WHERE type='run'").fetchall()}


print("-- backfill: canned rows land in calls + run graph --")
intent_map = {"oai-batch-1": "edge-typing", "anth-batch-1": "loinc-mapping"}
added, total = backfill.backfill(intent_map=intent_map)
check("3 rows added", added == 3)
check("dollars = 12.5 + 3 + 7.25 = 22.75", abs(total - 22.75) < 1e-9)
check("3 call rows written", _calls_count() == 3)
check("3 run nodes (id == batch id)", _run_nodes() == {"oai-batch-1", "oai-batch-2", "anth-batch-1"})

print("-- intent_map tags the matching rows --")
tags = [r[0] for r in calls._db().execute(
    "SELECT intent FROM calls WHERE caller='backfill:ledger' AND intent IS NOT NULL").fetchall()]
check("two intents tagged (edge-typing, loinc-mapping)", sorted(tags) == ["edge-typing", "loinc-mapping"])

print("-- backfill is idempotent: re-running ingests nothing new --")
added2, total2 = backfill.backfill(intent_map=intent_map)
check("0 added on re-run", added2 == 0)
check("$0 on re-run", total2 == 0.0)
check("still 3 call rows", _calls_count() == 3)
check("still 3 run nodes", len(_run_nodes()) == 3)

print("-- providers filter: only anthropic --")
# wipe and reset the db state by using a fresh batch id set — providers=('anthropic',) skips OpenAI reader
backfill._anthropic_rows = lambda: [
    ("anthropic", "claude-haiku-4-5", 0.40, 100_000, 5_000, "2026-06-03", "anth-batch-2")]
addedA, totalA = backfill.backfill(providers=("anthropic",))
check("only the new anthropic row added", addedA == 1)
check("anth-batch-2 in graph", "anth-batch-2" in _run_nodes())
check("oai readers NOT consulted (no new oai nodes)",
      not any(n.startswith("oai-batch-3") for n in _run_nodes()))

print("-- backfill with no intent_map (None) still works --")
backfill._openai_rows = lambda: [("openai", "gpt-5.5", 1.0, 100, 100, "2026-06-04", "oai-batch-3")]
backfill._anthropic_rows = lambda: []
addedN, _ = backfill.backfill(providers=("openai",))
check("untagged row added", addedN == 1)
row_intent = calls._db().execute(
    "SELECT intent FROM calls WHERE caller='backfill:ledger' ORDER BY ts DESC LIMIT 1").fetchone()
check("untagged row intent is NULL", row_intent[0] is None)

print("-- load_intent_map: flat {batch_id: intent} JSON file --")
d = tempfile.mkdtemp()
flat = os.path.join(d, "map.json")
json.dump({"b1": "intentA", "b2": "intentB"}, open(flat, "w"))
m = backfill.load_intent_map(flat)
check("flat map parses", m == {"b1": "intentA", "b2": "intentB"})

print("-- load_intent_map: a non-dict JSON file → empty map (never crashes) --")
notdict = os.path.join(d, "list.json")
json.dump([1, 2, 3], open(notdict, "w"))
check("non-dict JSON → {}", backfill.load_intent_map(notdict) == {})

print("-- load_intent_map: directory of *_batch_id.json files (stem = intent) --")
mdir = tempfile.mkdtemp()
# single-id form: {"id": ...}
json.dump({"id": "BX1"}, open(os.path.join(mdir, "edge_typing_batch_id.json"), "w"))
# multi-id form: {"ids": [...]}
json.dump({"ids": ["BX2", "BX3"]}, open(os.path.join(mdir, "loinc_map_batch_id.json"), "w"))
# a corrupt file is skipped, not fatal
open(os.path.join(mdir, "broken_batch_id.json"), "w").write("{not json")
# a non-.json file is ignored
open(os.path.join(mdir, "README.txt"), "w").write("ignore me")
dm = backfill.load_intent_map(mdir)
check("single-id → BX1 tagged edge_typing", dm.get("BX1") == "edge_typing")
check("multi-id → BX2/BX3 tagged loinc_map", dm.get("BX2") == "loinc_map" and dm.get("BX3") == "loinc_map")
check("corrupt file skipped (only 3 ids)", len(dm) == 3)

print("-- load_intent_map: missing path → empty map --")
check("missing path → {}", backfill.load_intent_map(os.path.join(d, "nope.json")) == {})

print("-- main(argv): dir intent-map path + providers, prints summary, returns 0 --")
backfill._openai_rows = lambda: [("openai", "gpt-5.5", 2.0, 1000, 1000, "2026-06-05", "BX1")]
backfill._anthropic_rows = lambda: []
rc = backfill.main(["--intent-map", mdir, "--providers", "openai"])
check("main returns 0", rc == 0)
bx1_intent = calls._db().execute("SELECT intent FROM calls WHERE caller='backfill:ledger' "
                                 "ORDER BY ts DESC LIMIT 1").fetchone()
check("main applied intent map (BX1 → edge_typing)", bx1_intent[0] == "edge_typing")

print("-- main(argv): no intent-map branch --")
backfill._openai_rows = lambda: [("openai", "gpt-5.5", 1.0, 1, 1, "2026-06-06", "BX9")]
check("main (no map) returns 0", backfill.main(["--providers", "openai"]) == 0)

print(f"\n{'[FAIL]' if failures else 'OK'} backfill: {failures} failure(s)")
sys.exit(1 if failures else 0)
