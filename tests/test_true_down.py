"""Offline tests for the ESTIMATE→ACTUAL TRUE-DOWN (ledger_sync.true_down) + the trust check's apples-to-apples
axes. The gate records batch cost at SUBMIT time (an estimate); the provider bills actuals per batch later. The
true-down nets the two per (provider, model) as negative correction rows — original estimate rows are never
mutated, corrections carry the REAL model + the conv_id sentinel so by_dims nets them before any push, and a
provider whose billed fetch failed is never trued down (unknown must not read as $0 billed). NO network: billed
rows are passed in / monkeypatched; provider fetchers are stubbed in the integration case.
"""
import os, sys, tempfile
if not os.environ.get("SPENDGUARD_TEST_ISOLATED"):
    os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"
    os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="spendguard-test-")
    os.execv(sys.executable, [sys.executable] + sys.argv)

from spendguard import budget
from spendguard import ledger_sync as LS
from spendguard import trust

failures = 0
def check(label, cond):
    global failures
    ok = bool(cond)
    if not ok: failures += 1
    print(f"  [{'OK' if ok else 'FAIL'}] {label}")


SINCE, D1, D2 = "2026-06-01", "2026-06-03", "2026-06-04"
SONNET, OPUS, G55 = "claude-sonnet-5", "claude-opus-4-8", "gpt-5.5"


def seed(day, provider, model, kind, cost, project, conv="conv-x"):
    budget._db().execute("INSERT INTO charges (ts,day,provider,model,kind,cost,project,conv_id) VALUES (?,?,?,?,?,?,?,?)",
                         (day + "T00:00:00+00:00", day, provider, model, kind, cost, project, conv))
    budget._db().commit()


def batch_total(exclude_reconciled=True):
    return round(sum(budget.by_day(kind="batch", since=SINCE, exclude_reconciled=exclude_reconciled).values()), 6)


def model_net(provider, model):
    r = budget._db().execute("SELECT COALESCE(SUM(cost),0) FROM charges WHERE kind='batch' AND provider=? AND model=?",
                             (provider, model)).fetchone()
    return round(float(r[0] or 0), 6)


print("-- marker drift guard: budget._RT_MARKERS must equal ledger_sync's realtime marker constants --")
check("budget._RT_MARKERS == {LS._RT_MARKER, LS._RT_ORACLE_MARKER, LS._RT_RECON_MARKER}",
      set(budget._RT_MARKERS) == {LS._RT_MARKER, LS._RT_ORACLE_MARKER, LS._RT_RECON_MARKER})

print("-- seed: gate batch ESTIMATES across 2 providers / 3 models / 2 projects --")
seed(D1, "anthropic", SONNET, "batch", 60.0, "lmm")
seed(D2, "anthropic", SONNET, "batch", 40.0, "healiom")     # sonnet est=100, billed 70 → Δ30 split 60:40
seed(D1, "anthropic", OPUS, "batch", 30.0, "lmm")           # opus est=30, billed 35 → UNDER-estimate: never trued
seed(D1, "openai", G55, "batch", 50.0, "lmm")               # openai est=50, billed $0 (failed batches) → trued to 0
BILLED = {
    "openai": [],                                            # fetch OK, genuinely nothing billed
    "anthropic": [("anthropic", SONNET, 70.0, 1_000_000, 50_000, D1, "b-a1"),
                  ("anthropic", OPUS, 35.0, 400_000, 20_000, D1, "b-a2")],
}

print("-- true_down: over-estimates come down to billed per (provider, model); under-estimates untouched --")
td = LS.true_down(since=SINCE, billed_rows=dict(BILLED))
check("trued_down ≈ $80 (sonnet 30 + openai 50)", abs(td["trued_down"] - 80.0) < 1e-6)
check("per-model detail present", abs(td["by_model"].get(f"anthropic:{SONNET}", 0) - 30.0) < 0.01
      and abs(td["by_model"].get(f"openai:{G55}", 0) - 50.0) < 0.01)
check("no provider skipped", td["skipped"] == [])
check("sonnet nets to billed $70", abs(model_net("anthropic", SONNET) - 70.0) < 1e-6)
check("opus (billed > est) untouched at $30", abs(model_net("anthropic", OPUS) - 30.0) < 1e-6)
check("openai nets to billed $0", abs(model_net("openai", G55)) < 1e-6)
check("batch total (excl reconciled) = $100", abs(batch_total() - 100.0) < 1e-6)

print("-- correction rows: negative, REAL model, conv_id sentinel, per-project/day proportional --")
rows = budget._db().execute("SELECT day, provider, model, cost, project FROM charges WHERE conv_id=?",
                            (budget._TRUE_DOWN_CONV,)).fetchall()
check("3 correction rows (2 sonnet cells + 1 openai cell)", len(rows) == 3)
check("all corrections negative + real model names", all(r[3] < 0 and not r[2].startswith("(") for r in rows))
cell = {(r[4], r[0]): r[3] for r in rows if r[2] == SONNET}
check("sonnet Δ30 split 60:40 → lmm −$18 @D1, healiom −$12 @D2",
      abs(cell.get(("lmm", D1), 0) + 18.0) < 0.01 and abs(cell.get(("healiom", D2), 0) + 12.0) < 0.01)
check("by_dims NETS to non-negative rows (server clamp never triggered)",
      all(r["cost"] >= -1e-9 for r in budget.by_dims(since=SINCE)))
dims = {(r["day"], r["model"], r["project"]): r["cost"] for r in budget.by_dims(since=SINCE) if r["kind"] == "batch"}
check("netted cells: (D1,sonnet,lmm)=$42, (D2,sonnet,healiom)=$28",
      abs(dims.get((D1, SONNET, "lmm"), 0) - 42.0) < 0.01 and abs(dims.get((D2, SONNET, "healiom"), 0) - 28.0) < 0.01)

print("-- idempotent: re-run with same billed truth = no-op --")
LS.true_down(since=SINCE, billed_rows=dict(BILLED))
check("batch total unchanged", abs(batch_total() - 100.0) < 1e-6)
check("still exactly 3 correction rows",
      budget._db().execute("SELECT COUNT(*) FROM charges WHERE conv_id=?", (budget._TRUE_DOWN_CONV,)).fetchone()[0] == 3)

print("-- trust: apples-to-apples axes + verdict flips red → green after true-down --")
seed(D1, "anthropic", SONNET, "realtime", 10.0, "lmm")                       # gate-live realtime (actual tokens)
seed(D1, "anthropic", LS._RT_RECON_MARKER, "realtime", 99.0, "lmm", conv="") # mirror row: must NOT count as recorded
truth = 105.0 + 10.0    # billed batch (70+35) + gate realtime log
check("BEFORE true-down the ratio was ALARM (estimates 180 vs billed 105)",
      trust.verdict(truth, 180.0 + 10.0)[0] == "alarm")
recorded = trust._ledger_llm_total(SINCE)
check("recorded excludes realtime mirror rows ($110, not $209)", abs(recorded - 110.0) < 1e-6)
check("AFTER true-down the verdict is ok", trust.verdict(truth, recorded)[0] == "ok")

print("-- in-flight batch self-heals: billed grows on the next run → correction shrinks --")
b2 = dict(BILLED); b2["openai"] = [("openai", G55, 20.0, 200_000, 10_000, D1, "b-o1")]
LS.true_down(since=SINCE, billed_rows=b2)
check("openai nets to the newly-billed $20", abs(model_net("openai", G55) - 20.0) < 1e-6)

print("-- failed billed fetch: that provider is NEVER trued down (unknown ≠ $0 billed) --")
b3 = dict(BILLED); b3["openai"] = None
td3 = LS.true_down(since=SINCE, billed_rows=b3)
check("openai reported skipped", td3["skipped"] == ["openai"])
check("openai estimates stand at $50 (no correction from unknown truth)", abs(model_net("openai", G55) - 50.0) < 1e-6)
check("anthropic still trued to billed $70", abs(model_net("anthropic", SONNET) - 70.0) < 1e-6)

print("-- versioned vs base model ids: JOIN normalizes (the live haiku bug); row keeps the original id --")
HAIKU_V, HAIKU_BASE = "claude-haiku-4-5-20251001", "claude-haiku-4-5"
budget._db().execute("DELETE FROM charges"); budget._db().commit()
seed(D1, "anthropic", HAIKU_V, "batch", 30.0, "lmm")         # gate recorded the DATED snapshot id
b4 = {"openai": [], "anthropic": [("anthropic", HAIKU_BASE, 24.0, 800_000, 40_000, D1, "b-h1")]}
td4 = LS.true_down(since=SINCE, billed_rows=b4)              # billed under the BASE name
check("normalized join nets versioned est to base-name billed ($24, not $0)",
      abs(model_net("anthropic", HAIKU_V) - 24.0) < 1e-6)
check("by_model reports under the normalized name", abs(td4["by_model"].get(f"anthropic:{HAIKU_BASE}", 0) - 6.0) < 0.01)
crow = budget._db().execute("SELECT model FROM charges WHERE conv_id=?", (budget._TRUE_DOWN_CONV,)).fetchone()
check("correction row carries the ORIGINAL (versioned) id so by_dims nets it", crow and crow[0] == HAIKU_V)

print("-- integration: reconcile_into_ledger runs true_down first; accounted == provider; trust stays ok --")
budget._db().execute("DELETE FROM charges"); budget._db().commit()
seed(D1, "anthropic", SONNET, "batch", 60.0, "lmm")
seed(D2, "anthropic", SONNET, "batch", 40.0, "healiom")      # est 100 vs billed 70 → TD 30, gap 0
import spendguard.report as report, spendguard.reconcile_anthropic as ra
import spendguard.backfill as backfill, spendguard.conv as conv, spendguard.saas as saas
report.openai_by_day = lambda: ({}, 0)
ra.cost_by_day = lambda since=None: ({D1: 70.0}, {})
backfill._openai_rows = lambda: []
backfill._anthropic_rows = lambda: [("anthropic", SONNET, 70.0, 1_000_000, 50_000, D1, "b-a1")]
conv.batch_project_map = lambda tdir=None: {}
saas.conn = lambda: {"enabled": True, "project": "lmm", "owns_account": True}
summ = LS.reconcile_into_ledger(since=SINCE)
check("summary carries the true-down stats", abs(summ["true_down"]["trued_down"] - 30.0) < 0.01)
check("no spurious gap rows (account net is 0 after true-down)", summ["gap_rows"] == 0)
check("accounted total == provider billed ($70)", abs(batch_total(exclude_reconciled=False) - 70.0) < 0.01)
recorded2 = trust._ledger_llm_total(SINCE)
check("trust verdict ok after reconcile+true_down (the push gate unblocks)",
      trust.verdict(70.0, recorded2)[0] == "ok")

print(f"\n{'[FAIL]' if failures else 'OK'} test_true_down: {failures} failure(s)")
sys.exit(1 if failures else 0)
