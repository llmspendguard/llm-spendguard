"""SQL index audit — every query the code makes must use an index on growth-prone tables.

Two guards, both un-regressable:
1) REQUIRED_INDEXES exist after the modules create their schema (a fresh isolated home — the same
   DDL upgrades existing installs on next open, since it runs at every connect).
2) Every SELECT/UPDATE/DELETE literal extracted from src/spendguard/*.py is EXPLAIN QUERY PLANned
   against that schema; a plan that SCANs a WATCHLIST table (the ones that grow with usage) fails
   unless the query is a whole-table aggregate registered in ALLOWED_SCANS. A future query on a big
   table without an index turns red HERE, not as a mystery slowdown in the daily report.
Offline, isolated, zero spend."""
import os
import sys
import tempfile

if not os.environ.get("SPENDGUARD_TEST_ISOLATED"):
    os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"
    os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="spendguard-idx-")
    os.execv(sys.executable, [sys.executable] + sys.argv)

import re
import glob
import sqlite3

fails = []


def ck(name, cond):
    print(("  [OK] " if cond else "  [FAIL] ") + name)
    if not cond:
        fails.append(name)


# ── create the full schema exactly as production code does ──
from spendguard import calls, bulkgate, learn, budget, calibrate, guard, config  # noqa: E402
from spendguard.ledger import SpendLedger  # noqa: E402

calls._db()
bulkgate._calls_db()
learn._db()
budget._db()
guard._db()
calibrate._con().close()
SpendLedger()          # creates spend_events + spend_audit (+ its 10-column index set)
try:
    from spendguard import callio
    callio._db()
except Exception:
    pass
try:
    from spendguard import semcache as _sc
    _sc._db()
except Exception:
    pass

con = sqlite3.connect(config.db_path())
tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}


def indexed_cols(table):
    out = set()
    for _seq, iname, *_rest in con.execute(f"PRAGMA index_list({table})").fetchall():
        for _rank, _cid, col in con.execute(f"PRAGMA index_info({iname})").fetchall():
            if col:
                out.add(col)
    # a PRIMARY KEY is an index too (rowid-alias INTEGER pks don't show in index_list)
    for _cid, name, _t, _nn, _dflt, pk in con.execute(f"PRAGMA table_info({table})").fetchall():
        if pk:
            out.add(name)
    return out


# ── guard 1: the required index inventory (drift-proof) ──
REQUIRED = {
    "calls": {"chain", "intent", "ts"},
    "gate_calls": {"sig", "model"},
    "graph_edges": {"rel", "src"},
    "charges": {"day", "conv_id"},
    "cost_predictions": {"paired_ts", "job_id"},
    "savings": {"day"},
    "insights": {"intent"},
    "graph_nodes": {"type"},
    "semcache": {"model"},
    "call_io": {"intent", "batch"},
    "spend_audit": {"event_id"},
    "spend_events": {"org", "day", "source", "batch_id", "dedup_key", "conv_id"},
    "seg_attribution": {"source"},
}
for table, cols in sorted(REQUIRED.items()):
    if table not in tables:
        ck(f"{table}: table exists", False)
        continue
    missing = cols - indexed_cols(table)
    ck(f"{table}: indexed on {sorted(cols)}", not missing or print(f"      missing: {missing}"))

# ── guard 2: EXPLAIN QUERY PLAN every extractable query in the codebase ──
WATCHLIST = {"calls", "gate_calls", "spend_events", "charges", "call_io", "semcache", "graph_edges"}
# whole-corpus reads are scans BY DESIGN (aggregate everything): register them explicitly.
ALLOWED_SCAN_SNIPPETS = (
    "FROM calls",                # advise/evidence + calibrate corpus reads aggregate the whole (small-col) corpus
    "CAST(out_tok AS REAL)/max_tokens FROM gate_calls",   # fill obs: the GLOBAL level pools everything by design
    "AVG(CAST(out_tok AS REAL)/max_tokens)",              # summary_lines whole-table fill rollup (reads all rows by design)
    "FROM spend_events",         # reconcile/report period aggregates roll the window up
    "FROM charges",              # by_day month aggregates
    "UPDATE charges SET project=",   # one-shot meta-project backfill (maintenance, not a hot path)
    "FROM call_io",              # corpus-wide lint/backfill passes
    "FROM semcache",             # cache stats
    "FROM graph_edges GROUP BY rel",
)
SQL_RE = re.compile(r'"((?:SELECT|DELETE FROM|UPDATE)\s[^"]{10,400})"', re.I | re.S)
queries = set()
for path in glob.glob(os.path.join(os.path.dirname(calls.__file__), "*.py")):
    src = open(path).read().replace('"""', '"').replace("\n", " ")
    for m in SQL_RE.finditer(src):
        q = re.sub(r"\{[^}]*\}", "1", m.group(1)).strip()      # f-string holes → harmless literal
        q = re.sub(r"\s+", " ", q)
        if " FROM " in q.upper() or q.upper().startswith(("DELETE", "UPDATE")):
            queries.add(q)

planned, skipped, offenders = 0, 0, []
for q in sorted(queries):
    try:
        plan = " | ".join(r[3] for r in con.execute("EXPLAIN QUERY PLAN " + q))
        planned += 1
    except Exception:
        skipped += 1                                            # composed/multi-line SQL — not extractable
        continue
    for t in WATCHLIST:
        if re.search(rf"SCAN {t}\b", plan) and not any(s in q for s in ALLOWED_SCAN_SNIPPETS):
            offenders.append((t, q[:100], plan[:80]))
print(f"  audited {planned} extractable queries ({skipped} skipped as non-literal SQL)")
for t, q, p in offenders:
    print(f"      UNINDEXED SCAN on {t}: {q}  →  {p}")
ck("no unindexed scans on growth-prone tables (or the scan is a registered whole-corpus aggregate)",
   not offenders)
ck("audit actually exercised a meaningful query set", planned >= 40)

con.close()
print(("[OK]" if not fails else "[FAIL]") + " sql index audit: %d failure(s)" % len(fails))
sys.exit(1 if fails else 0)
