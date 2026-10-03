"""ADDITIVE-COLUMN migration — a column appended to `_ADDITIVE_COLUMNS` after the v5 schema shipped must be
ALTER-ADDed to an EXISTING spend_events table, not only to a freshly-created one. CREATE TABLE IF NOT EXISTS never
adds a column to a table that already exists, so without the ALTER loop in `_ensure_schema` a deployed user's ledger
would silently drop every write to the new column (record_event binds only columns the table HAS).

This guards the MECHANISM, not one column: it strips EVERY `_ADDITIVE_COLUMNS` column out of the real DDL to build a
pre-additive (v5-era) ledger, opens it through the real code, and asserts each additive column lands. Then it proves
the NEWEST one (chain) is not merely present but writable+readable end-to-end — a chain-tagged charge recorded through
the normal gate path reads back via `budget.spent_by_job`. A new additive column therefore cannot ship without this
passing. Version-independent: builds the legacy table by removing column-definition lines (no ALTER DROP COLUMN, which
needs sqlite >= 3.35), so it runs the same on any sqlite.

Offline, isolated home, zero spend (direct charge record; no LLM)."""
import os
import re
import sqlite3
import sys
import tempfile

os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="sg-additive-mig-")
os.environ.setdefault("SPENDGUARD_TEST_ISOLATED", "1")
os.environ.setdefault("SPENDGUARD_NO_AUTOINSTALL", "1")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from spendguard import budget, calls, ledger  # noqa: E402

fails = []


def ck(name, cond, extra=""):
    print(("  [OK] " if cond else "  [FAIL] ") + name + (("  — " + extra) if extra and not cond else ""))
    if not cond:
        fails.append(name)


ADDITIVE = [c for c, _ in ledger._ADDITIVE_COLUMNS]           # the columns the ALTER loop is responsible for
ck("there are additive columns to guard (incl. chain)", ADDITIVE and "chain" in ADDITIVE, extra=repr(ADDITIVE))

# Legacy fixture = the REAL current DDL with every additive COLUMN DEFINITION line removed → a v5-era spend_events
# that predates all of them. Only the `<name> TYPE` definition lines are removed (comment lines create no columns and
# are harmless to leave); the final column (tags) is not additive, so no trailing-comma is produced.
_defn = re.compile(r"^\s*(" + "|".join(map(re.escape, ADDITIVE)) + r")\s+(TEXT|INTEGER|REAL|BLOB|NUMERIC)\b", re.I)
legacy_ddl = "\n".join(ln for ln in ledger._DDL_EVENTS.splitlines() if not _defn.match(ln))

dbp = os.path.join(os.environ["SPENDGUARD_HOME"], "spend.db")
con = sqlite3.connect(dbp)
con.executescript(legacy_ddl)
con.commit()
pre = {r[1] for r in con.execute("PRAGMA table_info(spend_events)")}
con.close()
ck("the legacy fixture starts WITHOUT any additive column (a true pre-additive ledger)",
   not (set(ADDITIVE) & pre), extra=f"present: {sorted(set(ADDITIVE) & pre)}")

# Opening through the real code runs _ensure_schema → the additive ALTER loop on the existing table.
led = ledger.SpendLedger()
post = {r[1] for r in led._conn.execute("PRAGMA table_info(spend_events)")}
missing = [c for c in ADDITIVE if c not in post]
ck("EVERY additive column is ALTER-ADDed to the existing table", not missing, extra=f"still missing: {missing}")

# The newest additive column (chain) must be WRITABLE + READABLE end-to-end on the migrated ledger — present-but-unbound
# would still silently drop writes, so prove the full record_charge → spent_by_job round-trip, not just the PRAGMA.
with calls.context(chain="legacy-ledger-job"):
    budget.record_charge("openai", "gpt-5-mini", "realtime", 0.07)
got = budget.spent_by_job("legacy-ledger-job")
ck("a chain-tagged charge round-trips through the MIGRATED ledger (0.07)", abs(got - 0.07) < 1e-9, extra=f"got={got}")

print(f"\n{'OK' if not fails else 'FAIL'} test_additive_columns_migrate_existing_ledger: {len(fails)} failure(s)")
sys.exit(1 if fails else 0)
