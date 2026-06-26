"""Ledger LEAK metric — guards the false-alarm bug found 2026-06-26: `doctor` cried "~$1.9k provider-billed batch
not in the ledger, install the gate" when in fact EVERY batch since recording began was accounted. Three compounding
errors produced the phantom: (1) the cutoff was the GLOBAL ledger_start (realtime-driven, weeks before batch
recording), dragging pre-batch history in-window; (2) the leak excluded the `(provider-batch)` reconcile backfill,
re-counting a gap a prior reconcile already absorbed; (3) it SUMMED per-day positive gaps, which double-counts the
day-spread artifact (reconcile caps accounted ≤ provider at the TOTAL but spreads the backfill across provider-usage
days, so an under-covered day is offset by an over-covered one). The fix: per-axis cutoff · leak vs ACCOUNTED ·
leak = NET (provider − accounted) total, not per-day sum · alarm points to reconcile (staleness) + coverage (ungated
sources), never "install the gate." Offline; provider side mocked; 0 spend."""
import os, sys, tempfile

if not os.environ.get("SPENDGUARD_TEST_ISOLATED"):
    os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"
    os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="spendguard-leak-")
    os.execv(sys.executable, [sys.executable] + sys.argv)

from spendguard import budget, ledger_sync
from spendguard.budget import _RECONCILED

fails = []
def ck(name, cond):
    print(("  [OK] " if cond else "  [FAIL] ") + name)
    if not cond:
        fails.append(name)


def _ins(day, provider, model, kind, cost):
    budget._db().execute("INSERT INTO charges (ts,day,provider,model,kind,cost,project) VALUES (?,?,?,?,?,?,?)",
                         (day + "T00:00:00+00:00", day, provider, model, kind, float(cost), "demo"))
    budget._db().commit()

# realtime since April → the GLOBAL ledger_start is realtime-driven (the trap: it predates batch recording)
_ins("2026-04-25", "openai", "gpt-5.5", "realtime", 12.0)
_ins("2026-06-20", "openai", "gpt-5.5", "realtime", 8.0)
# batch: prior reconcile backfilled provider truth as `(provider-batch)` rows, SPREAD across provider-usage days…
_ins("2026-06-03", "openai", _RECONCILED, "batch", 50.0)
_ins("2026-06-10", "openai", _RECONCILED, "batch", 800.0)    # 06-10 under-covers ($800 vs provider $1000)
_ins("2026-06-20", "openai", _RECONCILED, "batch", 400.0)
# …and the gate captured a slice LIVE on 06-20 (real model name, not the marker)
_ins("2026-06-20", "openai", "gpt-5.5", "batch", 300.0)      # 06-20 accounted = 400 + 300 = 700 vs provider $500 → over-covers
# accounted by day: 06-03=$50, 06-10=$800, 06-20=$700  → TOTAL $1550 (== provider total); per-day off by ±$200

# provider truth (mocked): $200 pre-batch-recording (2026-05-11) + June days summing to $1550
PROV = {"2026-05-11": 200.0, "2026-06-03": 50.0, "2026-06-10": 1000.0, "2026-06-20": 500.0}
ledger_sync._provider_batch_by_day = lambda since: ({d: v for d, v in PROV.items() if d >= since}, 0)

# ── per-axis ledger_start ──
ck("global ledger_start is realtime-driven (2026-04-25)", budget.ledger_start() == "2026-04-25")
ck("batch ledger_start is the batch axis's own first day (2026-06-03), NOT the global one",
   budget.ledger_start("batch") == "2026-06-03")

# ── the artifact case: net-accounted, but per-day gaps from day-spread (under $200 on 06-10, over $200 on 06-20) ──
c = ledger_sync._compute(since="2026-04-01")
ck("batch cutoff = batch ledger_start, not global", c["cutoff"] == "2026-06-03")
ck("LEAK = NET (provider − accounted) ≈ 0 — fully accounted overall", c["leak"] < 0.5)
ck("REGRESSION GUARD: leak is NOT the per-day positive-gap sum (~$200 would be the day-spread bug)", c["leak"] < 50)
ck("REGRESSION GUARD: leak is NOT the gate-capture shortfall (~$1250 would be the backfill-exclusion bug)", c["leak"] < 1.0)
ck("capture_rate reflects gate-LIVE only (~19% = $300/$1550), reported separately", 14 < c["capture_rate"] < 24)
ck("coverage (accounted/provider) ≈ 100%", c["coverage"] > 99)
ck("pre-batch-recording provider ($200 on 2026-05-11) is pre_ledger, NOT leak", abs(c["pre_ledger"] - 200.0) < 0.5)

line = ledger_sync.leak_line(since="2026-04-01")
ck("leak_line says accounted/no-material-leak (not an alarm)", line and "no material leak" in line and "behind" not in line)
ck("leak_line surfaces live-capture rate + points low capture to `coverage`", line and "captured" in line and "coverage" in line)

# ── a GENUINE net shortfall: provider billed a post-cutoff day with NO gate row and NO backfill (nothing offsets it) ──
PROV2 = dict(PROV); PROV2["2026-06-25"] = 400.0
ledger_sync._provider_batch_by_day = lambda since: ({d: v for d, v in PROV2.items() if d >= since}, 0)
c2 = ledger_sync._compute(since="2026-04-01")
ck("GENUINE net shortfall surfaces (~$400 provider in neither gate nor reconcile)", abs(c2["leak"] - 400.0) < 1.0)
line2 = ledger_sync.leak_line(since="2026-04-01")
ck("leak_line flags the shortfall + points to `saas reconcile` (refresh), not 'install the gate'",
   line2 and "behind" in line2 and "saas reconcile" in line2 and "install" not in line2)

print(("[OK]" if not fails else "[FAIL]") + " ledger-leak: %d failure(s)" % len(fails))
sys.exit(1 if fails else 0)
