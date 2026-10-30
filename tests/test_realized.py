"""Realized efficiency (realized.py) — MEASURED before/after $ per call around insight adoptions.
Guards: the before/after math on a seeded corpus; regressions shown (negative), never synced as savings;
sync into guarded(source=realized) is INCREMENTAL and idempotent (re-runs never double-record); thin
sides (<5 calls) not judged; report + CLI wiring. Offline, zero spend."""
import os, sys, tempfile

if not os.environ.get("SPENDGUARD_TEST_ISOLATED"):
    os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"
    os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="spendguard-realized-")
    os.execv(sys.executable, [sys.executable] + sys.argv)

import sqlite3
import unittest
from _script_checks import record_script_check
from spendguard import realized, config, learn, guard

fails = []
ADOPT = "2026-06-15T00:00:00"
REALIZED_THIN_AFTER_CALLS = 100
con = sqlite3.connect(config.db_path())
con.execute("""CREATE TABLE IF NOT EXISTS calls(
    id TEXT PRIMARY KEY, ts TEXT, chain TEXT, intent TEXT, caller TEXT, provider TEXT, model TEXT, kind TEXT,
    in_tok INTEGER, out_tok INTEGER, cost REAL, latency REAL,
    prompt_hash TEXT, prompt_snip TEXT, output_snip TEXT, finish TEXT,
    quality TEXT, quality_src TEXT, quality_conf REAL)""")
def call(i, intent, ts, cost):
    con.execute("INSERT INTO calls (id, ts, intent, cost) VALUES (?,?,?,?)", (f"{intent}-{i}", ts, intent, cost))
for i in range(6):
    call(i, "typing", f"2026-06-0{i+1}T00:00:00", 0.10)          # before: $0.10/call
for i in range(10):
    call(100 + i, "typing", f"2026-06-2{i % 10}T00:00:00", 0.04)  # after: $0.04/call → +$0.06 × 10 = $0.60
for i in range(6):
    call(i, "summarize", f"2026-06-0{i+1}T00:00:00", 0.02)
for i in range(8):
    call(100 + i, "summarize", f"2026-06-2{i % 10}T00:00:00", 0.05)  # REGRESSED: −$0.03/call
for i in range(realized.MIN_EACH - 1):
    call(i, "thin", f"2026-06-0{i+1}T00:00:00", 0.10)            # <5 before-calls → not judged
for i in range(REALIZED_THIN_AFTER_CALLS):
    call(REALIZED_THIN_AFTER_CALLS + i, "thin", ADOPT, 0.04)
con.commit(); con.close()
learn.add_insight("typing", "pack 30/req", evidence="test", confidence=0.8)
learn.add_insight("summarize", "use nano", evidence="test", confidence=0.8)
learn.add_insight("thin", "irrelevant", evidence="test", confidence=0.8)
# pin adoption timestamps deterministically (add_insight stamps now(); measurement anchors on ts)
con = sqlite3.connect(str(config.HOME / "learning.db")) if (config.HOME / "learning.db").exists() else None
import glob
for db in glob.glob(str(config.HOME / "*.db")):
    c = sqlite3.connect(db)
    try:
        c.execute("UPDATE insights SET ts = ?", (ADOPT,)); c.commit()
    except Exception:
        pass
    c.close()

with unittest.TestCase().assertLogs(realized.__name__, level="DEBUG") as realized_skip_logs:
    rows = realized.measure()
record_script_check(fails, "thin intent rejection names intent and below-minimum before count",
   any(record.args == ("thin", realized.MIN_EACH - 1, REALIZED_THIN_AFTER_CALLS,
                       realized.MIN_EACH, True, False, False, False)
       for record in realized_skip_logs.records))
by = {r["intent"]: r for r in rows}
record_script_check(fails, "improved intent measured (+$0.06/call × 10 = $0.60)",
   "typing" in by and abs(by["typing"]["realized_usd"] - 0.60) < 1e-6 and by["typing"]["after_calls"] == 10)
record_script_check(fails, "regression SHOWN, negative", "summarize" in by and by["summarize"]["realized_usd"] < 0)
record_script_check(fails, "thin history not judged", "thin" not in by)
record_script_check(fails, "ranked best-first", rows[0]["intent"] == "typing")

# ── incremental, idempotent sync into guarded(source=realized) ──
r1 = realized.sync_to_guarded()
record_script_check(fails, "first sync records only the POSITIVE delta ($0.60)", abs(r1["synced_usd"] - 0.60) < 1e-6 and r1["intents"] == 1)
r2 = realized.sync_to_guarded()
record_script_check(fails, "re-run syncs NOTHING new (idempotent)", r2["synced_usd"] == 0 and r2["intents"] == 0)
gcon = sqlite3.connect(str(config.HOME / "guarded.db")) if (config.HOME / "guarded.db").exists() else None
found = 0.0
for db in glob.glob(str(config.HOME / "*.db")):
    c = sqlite3.connect(db)
    try:
        found += c.execute("SELECT COALESCE(SUM(amount),0) FROM savings WHERE source='realized'").fetchone()[0]
    except Exception:
        pass
    c.close()
record_script_check(fails, "guarded savings table holds realized $0.60 exactly once", abs(found - 0.60) < 1e-6)
record_script_check(fails, "realized is a CERTAIN source with high confidence",
   "realized" in guard.CERTAIN and guard.CONFIDENCE.get("realized", 0) >= 0.85)

# ── new after-calls later → only the increment syncs ──
con = sqlite3.connect(config.db_path())
for i in range(5):
    call(200 + i, "typing", f"2026-06-29T0{i}:00:00", 0.04)
con.commit(); con.close()
r3 = realized.sync_to_guarded()
record_script_check(fails, "later calls sync only the increment (5 × $0.06 = $0.30)", abs(r3["synced_usd"] - 0.30) < 1e-6)

import inspect
from spendguard import report
from _script_checks import cli_dispatch_preserves_owner_call
record_script_check(fails, "CLI wired: `spendguard realized`",
                    cli_dispatch_preserves_owner_call("realized", "spendguard.realized:main", "none"))
record_script_check(fails, "report wired: realized sync + auto_fresh", "sync_to_guarded" in inspect.getsource(report.generate_report)
   and "auto_fresh" in inspect.getsource(report.generate_report))

print(("[OK]" if not fails else "[FAIL]") + " realized: %d failure(s)" % len(fails))
sys.exit(1 if fails else 0)
