"""record-call-outcome consolidation: record_call AND insert (bakeoff/backfill/titration) go through ONE attribution
brain (calls._attribute), so no INSERT path can be a second, ungated, un-attributed write — the DRIFT the capability
map found (insert used to skip the paid-call-no-intent enforcement AND never record a project).

Pins: (1) both paths ENFORCE intent on a paid row (raise under SPENDGUARD_REQUIRE_INTENT); (2) insert RECORDS the
project, exactly like record_call. Offline, isolated home, zero spend."""
import os
import sys
import tempfile

os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="spendguard-attr-")
os.environ.setdefault("SPENDGUARD_TEST_ISOLATED", "1")
os.environ.setdefault("SPENDGUARD_NO_AUTOINSTALL", "1")
os.environ["OPENAI_API_KEY"] = "sk-test-fake-not-real"
os.environ["ANTHROPIC_API_KEY"] = "sk-ant-test-fake-not-real"
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from spendguard import calls   # noqa: E402

fails = []


def ck(name, cond):
    print(("  [OK] " if cond else "  [FAIL] ") + name)
    if not cond:
        fails.append(name)


def _raises_on_paid_unintented(fn):
    try:
        fn("openai", "gpt-x", "realtime", 0.05)          # PAID, NO intent, no context → must be enforced
        return False
    except ValueError:
        return True


print("-- both write paths enforce intent on a paid row (one shared brain, not two policies) --")
os.environ["SPENDGUARD_REQUIRE_INTENT"] = "1"            # make the shared enforcement a hard raise (deterministic)
ck("record_call raises on a paid un-intented row", _raises_on_paid_unintented(calls.record_call))
ck("insert raises on the SAME paid un-intented row (shares calls._attribute — was ungated before)",
   _raises_on_paid_unintented(calls.insert))
os.environ.pop("SPENDGUARD_REQUIRE_INTENT", None)

print("\n-- insert records the PROJECT, exactly like record_call (attribution parity, not a bare INSERT) --")
cid = calls.insert("openai", "gpt-x", "realtime", 0.01, intent="test:insert-attr", project="MyRepo")
con = calls._calls_db()
row = con.execute("SELECT intent, project FROM calls WHERE id=?", (cid,)).fetchone()
ck("insert wrote the row with its intent", row and row[0] == "test:insert-attr")
ck("insert wrote the project (lowercased, like the money ledger) — no more un-attributed second INSERT",
   row and row[1] == "myrepo")

print("\n-- call class + stable process-session attribution reaches BOTH insert paths --")
calls.set_context(intent="outer-workload")
work_id = calls.record_call("openai", "gpt-x", "realtime", 0, call_class="probe")
with calls.gate_internal():
    meta_id = calls.insert("openai", "gpt-x", "realtime", 0)
rows = con.execute("SELECT id, call_class, origin_session, intent FROM calls WHERE id IN (?,?) ORDER BY id",
                   (work_id, meta_id)).fetchall()
ck("record_call accepts probe attribution", any(r[0] == work_id and r[1] == "probe" for r in rows))
ck("insert inherits gate_internal even under a workload intent",
   any(r[0] == meta_id and r[1] == "gate_internal" and r[3] == "outer-workload" for r in rows))
ck("every new row has the same stable per-process origin_session",
   len({r[2] for r in rows if r[2]}) == 1 and len(rows) == 2)

print("\n-- a legacy calls table migrates additively and legacy rows remain NULL --")
legacy_home = tempfile.mkdtemp(prefix="spendguard-legacy-calls-")
legacy_db = os.path.join(legacy_home, "legacy.db")
legacy = __import__("sqlite3").connect(legacy_db)
legacy.execute("""CREATE TABLE calls(id TEXT PRIMARY KEY, ts TEXT, chain TEXT, intent TEXT, caller TEXT,
 provider TEXT, model TEXT, kind TEXT, in_tok INTEGER, out_tok INTEGER, cost REAL, latency REAL,
 prompt_hash TEXT, prompt_snip TEXT, output_snip TEXT, finish TEXT, quality TEXT, quality_src TEXT)""")
legacy.execute("INSERT INTO calls (id,ts,model,kind,cost) VALUES ('old','2020-01-01','old-model','realtime',0)")
legacy.commit()
calls._ensure_calls_schema(legacy)
old = legacy.execute("SELECT call_class, origin_session FROM calls WHERE id='old'").fetchone()
ck("additive migration leaves both new columns NULL on the legacy row", old == (None, None))
legacy.close()

print(f"\n{'[FAIL]' if fails else 'OK'} test_record_paths_share_attribution: {len(fails)} failure(s)")
sys.exit(1 if fails else 0)
