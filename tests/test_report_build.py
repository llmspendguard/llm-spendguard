"""report.build_rows — the PURE transform extracted from the I/O-heavy report (fetch→transform→load). The report
used to assemble its rows + subtotals inline with the network fetches + prints; pulling the arithmetic out makes
it unit-testable with zero network. This is the decoupling pattern (the same map/reduce shape as reconcile).
Offline; no home needed (build_rows is pure)."""
import sys
from spendguard import report

fails = []
def ck(name, cond):
    print(("  [OK] " if cond else "  [FAIL] ") + name)
    if not cond:
        fails.append(name)

# per-source by-day maps (dollars) + the three window starts
oai = {"2026-06-22": 10.0, "2026-06-20": 5.0, "2026-06-01": 2.0}
an = {"2026-06-22": 3.0}
rt = {"2026-06-21": 1.0}
gpu = {"2026-06-22": 4.0}
tstr, week_start, month_start = "2026-06-22", "2026-06-16", "2026-06-01"

rows = report.build_rows(oai, an, rt, gpu, tstr, week_start, month_start)
by_name = {r[0]: r for r in rows}

ck("OpenAI row windows: today 10, 7d 15, month 17", by_name["OpenAI batch (gpt-5.5)"][1:] == (10.0, 15.0, 17.0))
ck("Real-time row: not in 'today' (its day precedes tstr), in 7d/month",
   by_name["Real-time (gate-logged)"][1:] == (0.0, 1.0, 1.0))

sub = by_name["LLM subtotal"]
ck("LLM subtotal = OpenAI + Anthropic + Real-time, per window",
   sub[1:] == (10.0 + 3.0 + 0.0, 15.0 + 3.0 + 1.0, 17.0 + 3.0 + 1.0))

total = rows[-1]
ck("grand total is the LAST row (what the alert threshold reads)", total[0].startswith("TOTAL"))
gpu_row = by_name["Remote compute (vast.ai GPU)"]
ck("grand TOTAL = LLM subtotal + remote compute, per window (the report adds up)",
   all(abs(total[i] - (sub[i] + gpu_row[i])) < 1e-9 for i in (1, 2, 3)))

# empty inputs → all-zero rows, never a crash (an offline/unreachable run)
z = report.build_rows({}, {}, {}, {}, tstr, week_start, month_start)
ck("empty maps → every window 0, total 0 (graceful)", all(r[1] == 0 and r[2] == 0 and r[3] == 0 for r in z))

print(("\n[FAIL] " if fails else "\n[OK] ") + f"report_build: {len(fails)} failure(s)")
sys.exit(1 if fails else 0)
