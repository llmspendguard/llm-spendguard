"""_ensure_schema must index the LIVE spend_events table, even when a leftover table squats the index NAMES.

The bug this guards (2026-09-03): the charges→spend_events cutover left a vestigial `spend_events_precutover`
table whose indexes still hold the canonical `idx_se_*` NAMES. `_ensure_schema` did
`CREATE INDEX IF NOT EXISTS idx_se_day ON spend_events(day)` — but IF NOT EXISTS keys off the index NAME across
the WHOLE schema, so the squatters made it a silent no-op and the live 356k-row money table ended up with ONLY
its primary-key autoindex. Every windowed read (the daily-cap spent_today on every gated call, per-conv, dedup)
therefore full-SCANned the table.

Invariant (axis-4 ABSENCE): after _ensure_schema, spend_events carries a single-column index on EVERY declared
column — decided by what is actually indexed ON spend_events (table-scoped PRAGMA), never by index name.
"""
import os, sys, tempfile, sqlite3

if not os.environ.get("SPENDGUARD_TEST_ISOLATED"):
    os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"
    os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="spendguard-idx-")
    os.execv(sys.executable, [sys.executable] + sys.argv)

from spendguard.ledger import SpendLedger, _INDEXES

fails = []
def ck(name, cond):
    print(("  [OK] " if cond else "  [FAIL] ") + name)
    if not cond:
        fails.append(name)

def cols_indexed_on(conn, table):
    """The set of columns covered by a SINGLE-column index ON `table` — table-scoped, so it is immune to another
    table squatting the same index names (that is the whole point of the bug)."""
    out = set()
    for row in conn.execute(f"PRAGMA index_list({table})"):
        info = conn.execute(f'PRAGMA index_info("{row[1]}")').fetchall()
        if len(info) == 1:                       # info rows are (seqno, cid, column_name)
            out.add(info[0][2])
    return out

d = tempfile.mkdtemp(prefix="sg-idxtest-")
db = os.path.join(d, "spend.db")

# Reproduce the post-cutover state: a leftover table that SQUATS the canonical idx_se_* index names.
c = sqlite3.connect(db)
decl = ", ".join(f"{ix} TEXT" for ix in _INDEXES)
c.execute(f"CREATE TABLE spend_events_precutover (id TEXT PRIMARY KEY, {decl})")
for ix in _INDEXES:
    c.execute(f"CREATE INDEX idx_se_{ix} ON spend_events_precutover({ix})")   # take the canonical names
c.commit()
ck("precutover squatters present before _ensure_schema",           # (the set also holds the PK autoindex col 'id')
   set(_INDEXES) <= cols_indexed_on(c, "spend_events_precutover"))
c.close()

# Now let the real schema-ensure run against the SAME file. It must index the LIVE table regardless.
led = SpendLedger(db_path=db)
covered = cols_indexed_on(led._conn, "spend_events")
missing = [ix for ix in _INDEXES if ix not in covered]
ck("spend_events indexed on EVERY declared column despite the name-squatters", not missing)
if missing:
    print("    MISSING single-column index on spend_events for:", missing)

# and we did NOT disturb the backup table's indexes to do it (no dropping the money-DB's history)
c2 = sqlite3.connect(db)
pre = cols_indexed_on(c2, "spend_events_precutover")
c2.close()
ck("precutover backup indexes left intact", set(_INDEXES) <= pre)

print(("FAIL: " + ", ".join(fails)) if fails else "all live-table index checks passed")
sys.exit(1 if fails else 0)
