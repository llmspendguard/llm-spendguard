"""Offline test for ledger_sync — local gate ledger vs PROVIDER billing (leak detection).

Every provider/network entry point is stubbed (no network, no LLM, no Admin key). We seed local
batch rows via the budget ledger under an isolated SPENDGUARD_HOME, feed canned per-day provider
totals, and assert the diff / leak / coverage figures.
"""
import os, sys, tempfile

if not os.environ.get("SPENDGUARD_TEST_ISOLATED"):
    os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"
    os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="spendguard-ledger-sync-")
    os.execv(sys.executable, [sys.executable] + sys.argv)

import json
from spendguard import ledger_sync as LS, budget

failures = 0


def check(label, cond):
    global failures
    ok = bool(cond)
    if not ok:
        failures += 1
    print(f"  [{'OK' if ok else 'FAIL'}] {label}")


# ── seed local gate-recorded batch spend over three days; provider stub will bill MORE on one day ──
DAYS = ["2026-06-01", "2026-06-02", "2026-06-03"]
# gate saw $10 on d1, $20 on d2, $0 on d3 (the leak day)
budget._db().execute("INSERT INTO charges (ts,day,provider,model,kind,cost,project) VALUES (?,?,?,?,?,?,?)",
                     (DAYS[0] + "T00:00:00+00:00", DAYS[0], "openai", "gpt-5.5", "batch", 10.0, "nlp-pipeline"))
budget._db().execute("INSERT INTO charges (ts,day,provider,model,kind,cost,project) VALUES (?,?,?,?,?,?,?)",
                     (DAYS[1] + "T00:00:00+00:00", DAYS[1], "openai", "gpt-5.5", "batch", 20.0, "nlp-pipeline"))
# a realtime + a meta row so the kind filters are exercised
budget._db().execute("INSERT INTO charges (ts,day,provider,model,kind,cost,project) VALUES (?,?,?,?,?,?,?)",
                     (DAYS[1] + "T00:00:00+00:00", DAYS[1], "openai", "gpt-5.5", "realtime", 4.0, "nlp-pipeline"))
budget._db().execute("INSERT INTO charges (ts,day,provider,model,kind,cost,project) VALUES (?,?,?,?,?,?,?)",
                     (DAYS[1] + "T00:00:00+00:00", DAYS[1], "anthropic", "claude-opus-4-8", "meta", 1.5, "llmseg"))
budget._db().commit()

SINCE = DAYS[0]
# provider billed: d1 $10 (matches), d2 $20 (matches), d3 $30 (gate saw $0 → LEAK $30)
PROV = {DAYS[0]: 10.0, DAYS[1]: 20.0, DAYS[2]: 30.0}
PENDING = 7


def stub_provider(since):
    return dict(PROV), PENDING


print("-- _provider_batch_by_day: REAL fn, inner provider readers stubbed (no network) --")
from spendguard import report
from spendguard import reconcile_anthropic as _anth
report.openai_by_day = lambda: ({DAYS[0]: 10.0, "2026-05-01": 99.0}, 5)   # incl. a pre-`since` day
_anth.cost_by_day = lambda since=None: ({DAYS[1]: 20.0}, {})
pb, pend = LS._provider_batch_by_day(SINCE)
check("merges openai+anthropic per day", abs(pb.get(DAYS[0], 0) - 10.0) < 1e-9 and abs(pb.get(DAYS[1], 0) - 20.0) < 1e-9)
check("drops days before `since`", "2026-05-01" not in pb)
check("pending passthrough from openai", pend == 5)

print("-- _provider_batch_by_day: provider errors fall back to empty (never crashes) --")
def _oai_boom():
    raise RuntimeError("oai down")
def _anth_boom(since=None):
    raise RuntimeError("anth down")
report.openai_by_day = _oai_boom
_anth.cost_by_day = _anth_boom
pb2, pend2 = LS._provider_batch_by_day(SINCE)
check("both providers down → empty dict", pb2 == {})
check("both providers down → pending 0", pend2 == 0)

LS._provider_batch_by_day = stub_provider   # NO network: canned per-day provider totals

print("-- _compute: diff / leak / coverage from canned provider + seeded local --")
c = LS._compute(since=SINCE)
check("since echoed", c["since"] == SINCE)
check("prov dict carried through", c["prov"] == PROV)
check("local_batch d1=10", abs(c["local_batch"].get(DAYS[0], 0) - 10.0) < 1e-9)
check("local_batch d2=20", abs(c["local_batch"].get(DAYS[1], 0) - 20.0) < 1e-9)
check("local_rt d2=4", abs(c["local_rt"].get(DAYS[1], 0) - 4.0) < 1e-9)
check("meta d2=1.5", abs(c["meta"].get(DAYS[1], 0) - 1.5) < 1e-9)
check("pending passthrough", c["pending"] == PENDING)
check("post_p = 60 (all >= cutoff)", abs(c["post_p"] - 60.0) < 1e-9)
check("post_l = 30 (gate batch only)", abs(c["post_l"] - 30.0) < 1e-9)
check("leak = 30 (d3 billed, not gated)", abs(c["leak"] - 30.0) < 1e-9)
check("coverage = 50%", abs(c["coverage"] - 50.0) < 1e-9)

print("-- leak_line: emits the ⚠ alert when leak > $0.5 --")
line = LS.leak_line(since=SINCE)
check("leak_line returns a string", isinstance(line, str))
check("leak_line flags LEDGER LEAK", "LEDGER LEAK" in line)
check("leak_line shows the ~$30 figure", "$30.00" in line)

print("-- leak_line: clean case (provider == local everywhere) --")
LS._provider_batch_by_day = lambda since: ({DAYS[0]: 10.0, DAYS[1]: 20.0}, 0)
clean = LS.leak_line(since=SINCE)
check("no-leak line mentions coverage", clean is not None and "coverage" in clean)
check("no-leak line is not a LEAK alert", "LEDGER LEAK" not in clean)

print("-- leak_line: None when there is nothing to compare --")
LS._provider_batch_by_day = lambda since: ({}, 0)
check("empty provider → None", LS.leak_line(since=SINCE) is None)

print("-- leak_line: None when _compute raises --")
def boom(since):
    raise RuntimeError("provider down")
LS._provider_batch_by_day = boom
check("exception → None (never crashes the report)", LS.leak_line(since=SINCE) is None)

print("-- sync: prints the table + returns totals; leak path --")
LS._provider_batch_by_day = stub_provider
r = LS.sync(since=SINCE)
check("sync provider total = 60", abs(r["provider"] - 60.0) < 1e-9)
check("sync local total = 30", abs(r["local"] - 30.0) < 1e-9)
check("sync coverage = 50%", abs(r["coverage"] - 50.0) < 1e-9)
check("sync leak = 30", abs(r["leak"] - 30.0) < 1e-9)

print("-- sync: clean / over-recorded / no-leak branches --")
# local > provider on a day → over-recorded branch; provider==local elsewhere → ok / no material leak
LS._provider_batch_by_day = lambda since: ({DAYS[0]: 5.0, DAYS[1]: 20.0}, 3)
r2 = LS.sync(since=SINCE)
check("sync clean leak ~0", r2["leak"] < 0.5)
check("sync clean coverage >= 100% (local>=provider)", r2["coverage"] >= 100.0 - 1e-9)

print("-- sync: pre-ledger gap branch (since before ledger_start) --")
EARLY = "2026-05-15"
LS._provider_batch_by_day = lambda since: ({EARLY: 99.0, DAYS[0]: 10.0, DAYS[1]: 20.0}, 0)
r3 = LS.sync(since=EARLY)   # ledger_start (2026-06-01) > since → pre-ledger rows excluded from post_*
check("pre-ledger provider not counted post-cutoff", abs(r3["coverage"] - 100.0) < 1e-9)

print("-- sync: no provider billing at all branch --")
LS._provider_batch_by_day = lambda since: ({}, 0)
r4 = LS.sync(since="2026-07-01")
check("empty provider → 100% coverage default", abs(r4["coverage"] - 100.0) < 1e-9)
check("empty provider → no leak", r4["leak"] == 0.0)

print("-- audit_completeness: stubbed OpenAI batch enumeration + Anthropic cache (no network) --")
from spendguard import reconcile_openai as ro, reconcile_anthropic as ra
ro.load_key = lambda: "sk-test"
ro.fetch_batches = lambda k: [
    {"id": "b-counted", "status": "completed", "model": "gpt-5.5",
     "usage": {"input_tokens": 1_000_000, "output_tokens": 0}, "request_counts": {"completed": 5}},
    {"id": "b-zero", "status": "cancelled", "model": "gpt-5.5",
     "usage": {}, "request_counts": {"completed": 0}},
    {"id": "b-unaccounted", "status": "completed", "model": "gpt-5.5",
     "usage": {}, "request_counts": {"completed": 3}},   # completed but no usage → REAL gap
]
# seed an anthropic usage cache the audit reads off disk
os.makedirs(os.path.dirname(ra.CACHE_PATH), exist_ok=True)
json.dump({"anth-1": {"cost": 2.5}, "anth-2": {"cost": 1.0}}, open(ra.CACHE_PATH, "w"))

audit = LS.audit_completeness()
check("openai total batches = 3", audit["openai"]["total"] == 3)
check("openai counted = 1", audit["openai"]["counted"] == 1)
check("openai zero_cost = 1", audit["openai"]["zero_cost"] == 1)
check("openai counted_usd = 2.5 (1M batch in)", abs(audit["openai"]["counted_usd"] - 2.5) < 1e-9)
check("openai unaccounted = [b-unaccounted]", audit["openai"]["unaccounted"] == ["b-unaccounted"])
check("anthropic batches = 2", audit["anthropic"]["batches"] == 2)
check("anthropic counted_usd = 3.5", abs(audit["anthropic"]["counted_usd"] - 3.5) < 1e-9)
check("complete = False (an unaccounted batch exists)", audit["complete"] is False)

print("-- audit_completeness: complete=True when no unaccounted --")
ro.fetch_batches = lambda k: [
    {"id": "b1", "status": "completed", "model": "gpt-5.5",
     "usage": {"input_tokens": 1_000_000, "output_tokens": 0}, "request_counts": {"completed": 1}},
]
audit2 = LS.audit_completeness()
check("complete = True", audit2["complete"] is True)
check("no unaccounted ids", audit2["openai"]["unaccounted"] == [])

print("-- audit_completeness: provider errors captured per-provider (never crashes) --")
def _key_boom():
    raise RuntimeError("no openai key")
ro.load_key = _key_boom
import spendguard.reconcile_anthropic as _ra2
_ra2_cache = _ra2.CACHE_PATH
_ra2.CACHE_PATH = None        # os.path.exists(None) raises inside → anthropic error branch
audit_err = LS.audit_completeness()
check("openai error surfaced", "error" in audit_err["openai"])
check("anthropic error surfaced", "error" in audit_err["anthropic"])
# on an openai error there's no 'unaccounted' list → `not None` → complete reports True (no gap seen)
check("complete True when openai errored (no unaccounted seen)", audit_err["complete"] is True)
_ra2.CACHE_PATH = _ra2_cache
ro.load_key = lambda: "sk-test"

print("-- reconcile_into_ledger: fully stubbed providers → per-project gap rows, idempotent --")
# stub every provider/network source the function reaches into
from spendguard import report, backfill, conv, callio, saas
report.openai_by_day = lambda: ({DAYS[1]: 40.0}, 0)
ra.cost_by_day = lambda since=None: ({DAYS[1]: 5.0}, {})
# evidence rows (provider-billed) used for per-project attribution; no conversation links → fallback project
backfill._openai_rows = lambda: [("openai", "gpt-5.5", 40.0, 1_000_000, 0, DAYS[1], "bx-oai")]
backfill._anthropic_rows = lambda: [("anthropic", "claude-opus-4-8", 5.0, 100, 100, DAYS[1], "bx-anth")]
conv.batch_links = lambda tdir=None: {}                 # no transcript indexing
callio._db = lambda: budget._db()                       # call_io lives in the isolated db (empty)
saas.conn = lambda: {"project": "nlp-pipeline", "projects": ["nlp-pipeline"]}   # single-project → fallback = 'nlp-pipeline'

summ = LS.reconcile_into_ledger(since=SINCE)
check("provider_total = 45 (40 + 5)", abs(summ["provider_total"] - 45.0) < 1e-9)
check("gate_attributed = 30 (nlp-pipeline batch the gate saw)", abs(summ["gate_attributed"] - 30.0) < 1e-9)
check("ungoverned gap = 15 (45 - 30)", abs(summ["ungoverned"] - 15.0) < 1e-9)
check("gap_rows = 1", summ["gap_rows"] == 1)
check("gap attributed to nlp-pipeline", abs(summ["gap_by_project"].get("nlp-pipeline", 0) - 15.0) < 1e-9)
check("no provider errors", summ["errors"] == {})
check("both providers ok", set(summ["providers_ok"]) == {"openai", "anthropic"})

# idempotent: a reconciled row was written, and re-running rebuilds (not double-counts)
recon_rows = budget._db().execute(
    "SELECT COUNT(*) FROM charges WHERE model='(provider-batch)'").fetchone()[0]
check("one reconciled row written", recon_rows == 1)
summ2 = LS.reconcile_into_ledger(since=SINCE)
recon_rows2 = budget._db().execute(
    "SELECT COUNT(*) FROM charges WHERE model='(provider-batch)'").fetchone()[0]
check("still one reconciled row after re-run (idempotent)", recon_rows2 == 1)
check("same gap on re-run", abs(summ2["ungoverned"] - 15.0) < 1e-9)

print("-- reconcile_into_ledger: provider fetch errors are surfaced, not hidden --")
def oai_boom():
    raise RuntimeError("openai 500")
def anth_boom(since=None):
    raise RuntimeError("anthropic 500")
report.openai_by_day = oai_boom
ra.cost_by_day = anth_boom
summ3 = LS.reconcile_into_ledger(since=SINCE)
check("openai error recorded", "openai" in summ3["errors"])
check("anthropic error recorded", "anthropic" in summ3["errors"])
check("both dropped from providers_ok", summ3["providers_ok"] == [])

print("-- reconcile_into_ledger: per-project attribution via conversation link + b2i intent, day<since skipped --")
report.openai_by_day = lambda: ({DAYS[1]: 40.0}, 0)
ra.cost_by_day = lambda since=None: ({}, {})
backfill._openai_rows = lambda: [
    ("openai", "gpt-5.5", 40.0, 1_000_000, 0, DAYS[1], "bx-linked"),   # attributed via conv link → vision
    ("openai", "gpt-5.5", 5.0, 100, 100, "2026-05-01", "bx-old"),       # day < since → skipped (line 141)
    ("openai", "gpt-5.5", 9.0, 100, 100, DAYS[1], "bx-intent"),         # attributed via b2i intent text → vision
    # the gate-recorded nlp batch IS also provider-billed (every gated batch is). Including it keeps the fixture
    # realistic (provider_total ≥ gate_total) so the double-count cap is a no-op here — gate $30 + vision $49 = $79.
    ("openai", "gpt-5.5", 30.0, 1_000_000, 0, DAYS[1], "bx-nlp"),       # no link/intent → fallback nlp-pipeline
]
backfill._anthropic_rows = lambda: []
# bx-linked → a snippet that _project_of routes to 'vision-pipeline'
conv.batch_links = lambda tdir=None: {"bx-linked": {"snippet": "video segment caption frames"}}
# bx-intent → an intent string that routes to 'vision-pipeline' via the b2i path
callio._db = lambda: budget._db()
budget._db().execute("CREATE TABLE IF NOT EXISTS call_io (batch TEXT, intent TEXT)")
budget._db().execute("DELETE FROM call_io")
# route the b2i intent to vision-pipeline too, so the attributed gap survives the gate-spend subtraction
budget._db().execute("INSERT INTO call_io (batch, intent) VALUES (?,?)", ("bx-intent", "video caption frames"))
budget._db().commit()
saas.conn = lambda: {"project": "nlp-pipeline", "projects": ["nlp-pipeline"]}
summ_attr = LS.reconcile_into_ledger(since=SINCE)
# bx-linked ($40, conv link) + bx-intent ($9, b2i) both → vision-pipeline (no gate spend there) → a real gap row
check("vision-pipeline gap from link + b2i intent = $49", abs(summ_attr["gap_by_project"].get("vision-pipeline", 0) - 49.0) < 1e-9)
check("nlp-pipeline has gate spend = provider → its gap suppressed (not in bucket)", "nlp-pipeline" not in summ_attr["gap_by_project"])
check("provider_total = $79 (vision $49 + nlp $30; pre-since bx-old excluded)", abs(summ_attr["provider_total"] - 79.0) < 1e-9)
check("NO double-count: gate_attributed + ungoverned ≤ provider_total", summ_attr["gate_attributed"] + summ_attr["ungoverned"] <= summ_attr["provider_total"] + 0.01)

print("-- reconcile_into_ledger: cross-classifier mismatch is CAPPED at provider truth (no double-count) --")
# gate recorded $30 under nlp (the seed). Provider evidence attributes the SAME $30 to vision (different classifier).
# Without the account-net cap: ledger = gate $30 + reconciled $30 = $60 = 2× the real $30. The cap holds it to $30.
report.openai_by_day = lambda: ({DAYS[1]: 30.0}, 0)
backfill._openai_rows = lambda: [("openai", "gpt-5.5", 30.0, 100, 0, DAYS[1], "bx-vis")]
conv.batch_links = lambda tdir=None: {"bx-vis": {"snippet": "video segment caption frames"}}   # → vision-pipeline
budget._db().execute("DELETE FROM call_io"); budget._db().commit()
summ_cap = LS.reconcile_into_ledger(since=SINCE)
check("double-count capped: gate + reconciled ≤ provider ($30, not $60)",
      summ_cap["gate_attributed"] + summ_cap["ungoverned"] <= summ_cap["provider_total"] + 0.01)
check("vision gap scaled toward 0 (account net = provider − gate = 0)", summ_cap["gap_by_project"].get("vision-pipeline", 0) < 0.5)

print("-- reconcile_into_ledger: multi-project saas → 'unattributed' fallback bucket --")
report.openai_by_day = lambda: ({DAYS[1]: 40.0}, 0)
ra.cost_by_day = lambda since=None: ({}, {})
backfill._openai_rows = lambda: [("openai", "gpt-5.5", 40.0, 1_000_000, 0, DAYS[1], "bx-multi")]
backfill._anthropic_rows = lambda: []
saas.conn = lambda: {"projects": ["nlp-pipeline", "vision-pipeline"]}   # >1 project → fallback = 'unattributed'
summ4 = LS.reconcile_into_ledger(since=SINCE)
check("no-evidence gap lands in 'unattributed'", "unattributed" in summ4["gap_by_project"])

print("-- reconcile_into_ledger: saas.conn + callio._db raising → tolerated (except paths) --")
def conn_boom():
    raise RuntimeError("no saas")
def db_boom():
    raise RuntimeError("no call_io db")
saas.conn = conn_boom
callio._db = db_boom        # the b2i intent lookup try/except → empty b2i
summ5 = LS.reconcile_into_ledger(since=SINCE)
check("saas error → still attributes a gap", summ5["ungoverned"] > 0)
check("callio._db error tolerated (b2i empty)", isinstance(summ5["gap_by_project"], dict))

print("-- reconcile_into_ledger: connected NON-owner (owns_account=false) skips the shared-account gap --")
report.openai_by_day = lambda: ({DAYS[1]: 40.0}, 0)
ra.cost_by_day = lambda since=None: ({}, {})
backfill._openai_rows = lambda: [("openai", "gpt-5.5", 40.0, 1_000_000, 0, DAYS[1], "bx-shared")]
backfill._anthropic_rows = lambda: []
callio._db = lambda: budget._db()   # restore (db_boom was set just above)
conv.batch_links = lambda tdir=None: {}
saas.conn = lambda: {"enabled": True, "project": "vision-pipeline", "owns_account": False}
summ_no = LS.reconcile_into_ledger(since=SINCE)
check("non-owner reconcile is skipped", bool(summ_no.get("skipped")))
check("non-owner records NO gap rows", summ_no["gap_rows"] == 0 and summ_no["gap_by_project"] == {})
saas.conn = lambda: {"enabled": True, "project": "vision-pipeline", "owns_account": True}
summ_own = LS.reconcile_into_ledger(since=SINCE)
check("owner DOES reconcile the shared gap (not skipped)", not summ_own.get("skipped"))

print("-- reconcile_realtime: backfill realtime_log → ledger gap (idempotent) --")
from spendguard import config as _cfg
import spendguard.saas as _saas_rt
_saas_rt.conn = lambda: {"project": "vision-pipeline"}   # single-project → fallback = vision-pipeline
RT_D1, RT_D2 = "2026-06-05", "2026-06-06"
with open(_cfg.RT_LOG, "w") as _f:
    _f.write(json.dumps({"day": RT_D1, "provider": "anthropic", "model": "claude-opus-4-8", "calls": 2, "cost": 3.0}) + "\n")
    _f.write(json.dumps({"day": RT_D2, "provider": "anthropic", "model": "claude-opus-4-8", "calls": 1, "cost": 5.0}) + "\n")
# the ledger already has $5 gate realtime on RT_D2 (no gap); RT_D1 has none → full $3 stranded gap
budget._db().execute("INSERT INTO charges (ts,day,provider,model,kind,cost,project) VALUES (?,?,?,?,?,?,?)",
                     (RT_D2 + "T00:00:00+00:00", RT_D2, "anthropic", "claude-opus-4-8", "realtime", 5.0, "vision-pipeline"))
budget._db().commit()
r1 = LS.reconcile_realtime(since=RT_D1)
check("reconcile_realtime: imports only the RT_D1 stranded gap ($3, 1 row)", abs(r1["imported"] - 3.0) < 1e-6 and r1["rows"] == 1)
_mk = budget._db().execute("SELECT COALESCE(SUM(cost),0), MAX(project) FROM charges WHERE kind='realtime' AND model=?", (LS._RT_MARKER,)).fetchone()
check("reconcile_realtime: marker row carries $3", abs(_mk[0] - 3.0) < 1e-6)
check("reconcile_realtime: gap attributed to the connection's project", _mk[1] == "vision-pipeline")
r2 = LS.reconcile_realtime(since=RT_D1)   # rebuild → must not double-count
_mk2 = budget._db().execute("SELECT COALESCE(SUM(cost),0) FROM charges WHERE kind='realtime' AND model=?", (LS._RT_MARKER,)).fetchone()[0]
check("reconcile_realtime: idempotent on re-run", abs(_mk2 - 3.0) < 1e-6 and abs(r2["imported"] - 3.0) < 1e-6)

print("-- main(argv): exercises the CLI dry path (sync) --")
LS._provider_batch_by_day = stub_provider
check("main returns 0", LS.main(["--since", SINCE]) == 0)

print(f"\n{'[FAIL]' if failures else 'OK'} ledger_sync: {failures} failure(s)")
sys.exit(1 if failures else 0)
