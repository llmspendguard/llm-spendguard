"""Guard for the cost×token cross-check (cost_integrity), offline + $0.

Pins the design that answers three honestreview doctrine findings:
  • NO hand-picked 'suspicious' threshold — the ONLY bound is the pricing model's own arithmetic ceiling (every
    input token billed as a 1h cache-write + output at list). A cost ABOVE that is provably impossible under any
    caching, so flagging it is a fact, not a judgement. Costs BELOW the ceiling are NOT flagged (ambiguous w/o the
    cache split) — no 0.33x-style band.
  • NO absolute-size floor hiding a large relative discrepancy — a tiny bucket billed above its ceiling is flagged.
  • A ledger READ FAILURE is UNKNOWN, never rendered green ('cannot tell' ≠ 'clean'); unpriced buckets are counted.

Nothing spends; prices come from the real offline pricing table, and the ledger read is monkeypatched for the
render cases."""
import os
import sys
import tempfile

os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="sg-costint-")
os.environ.setdefault("SPENDGUARD_TEST_ISOLATED", "1")
os.environ.setdefault("SPENDGUARD_NO_AUTOINSTALL", "1")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

import spendguard  # noqa: E402
spendguard.require = lambda: None
from spendguard import cost_integrity as ci  # noqa: E402

fails = []


def ck(name, cond, extra=""):
    print(("  [OK] " if cond else "  [FAIL] ") + name + (("  — " + extra) if extra and not cond else ""))
    if not cond:
        fails.append(name)


# opus-4-8 list: $5/M in, $25/M out. realtime ceiling(1000,500) = 0.005*2.0 + 0.0125 = $0.0225.
# batch (half rates): ceiling(1000,500) = 0.0025*2.0 + 0.00625 = $0.01125.
M = "claude-opus-4-8"
rows = [
    (M, "2026-10-04", "realtime", 40, 0.05, 1000, 500),    # $0.05 > $0.0225 ceiling → IMPOSSIBLE (flag, ~2.2x)
    (M, "2026-10-04", "realtime", 10, 0.02, 1000, 500),    # $0.02 < $0.0225 ceiling → within cache-write bound (omit)
    (M, "2026-10-01", "realtime", 7, 1.0, 0, 0),           # cost on ~zero priceable tokens → IMPOSSIBLE (flag, ratio None)
    (M, "2026-10-02", "batch", 20, 0.02, 1000, 500),       # $0.02 > $0.01125 BATCH ceiling → flag (realtime ceiling
    #                                                         $0.0225 would MISS it → proves the batch rate is used)
    ("totally-made-up-xyz", "2026-10-04", "realtime", 5, 99.0, 1000, 500),  # unpriced → counted, never flagged
]
found, unpriced = ci.classify_impossible_cost_buckets(rows)
by = {(f["day"], f["kind"]): f for f in found}

print("-- only PROVABLE over-ceiling buckets are flagged --")
ck("cost above the realtime ceiling → flagged", ("2026-10-04", "realtime") in by,
   extra=str([(f["day"], f["kind"]) for f in found]))
ck("that flag's ratio is ~2.2x the ceiling", (by.get(("2026-10-04", "realtime"), {}).get("ratio") or 0) > 2.0)
ck("cost WITHIN the cache-write ceiling → NOT flagged (no 'suspicious' band)",
   not any(f["day"] == "2026-10-04" and f["kind"] == "realtime" and abs(f["ledger_cost"] - 0.02) < 1e-9 for f in found))
ck("positive cost on ~zero priceable tokens → flagged (ratio None)",
   by.get(("2026-10-01", "realtime"), {}).get("ratio") is None and ("2026-10-01", "realtime") in by)
ck("BATCH bucket judged at the BATCH ceiling (realtime ceiling would miss $0.02) → flagged",
   ("2026-10-02", "batch") in by, extra=str([(f["day"], f["kind"]) for f in found]))
ck("unpriced model is COUNTED, not flagged (cannot bound it)",
   unpriced == 1 and not any(f["model"] == "totally-made-up-xyz" for f in found))

print("-- worst-first ordering (highest ratio first; ratio-None extremes lead) --")
ratios = [(f["ratio"] if f["ratio"] is not None else float("inf")) for f in found]
ck("findings sorted by ratio descending", ratios == sorted(ratios, reverse=True), extra=str(ratios))

print("-- render: read failure is UNKNOWN, never green --")
ci.audit_metered_cost_vs_tokens = lambda since_days=7: {"ok": False, "error": "no such column: suspect",
                                                        "findings": [], "unpriced": 0, "checked": 0}
lines = ci.cost_integrity_lines()
ck("read failure → ⚪ UNKNOWN line that says it is NOT clean",
   any(l.startswith("⚪") and "UNKNOWN" in l and "Not the same as clean" in l for l in lines), extra=str(lines))

print("-- render: clean states COVERAGE (checked / unpriced), not a bare green --")
ci.audit_metered_cost_vs_tokens = lambda since_days=7: {"ok": True, "findings": [], "unpriced": 2, "checked": 10}
lines = ci.cost_integrity_lines()
ck("clean 🟢 line names buckets checked AND unpriced",
   any(l.startswith("🟢") and "10 metered" in l and "2 unpriced" in l for l in lines), extra=str(lines))

print("-- render: a provable finding is a loud UNDER-RECORDED line --")
ci.audit_metered_cost_vs_tokens = lambda since_days=7: {"ok": True, "unpriced": 0, "checked": 3, "findings": [
    {"model": M, "day": "2026-10-04", "kind": "realtime", "n": 40, "ledger_cost": 0.05, "ceiling": 0.0225, "ratio": 2.22}]}
lines = ci.cost_integrity_lines()
ck("finding → 🔴 line stating UNDER-RECORDED above the ceiling",
   any(l.startswith("🔴") and "UNDER-RECORDED" in l and "ceiling" in l for l in lines), extra=str(lines))

print(f"\n{'OK' if not fails else 'FAIL'} test_cost_integrity_crosscheck: {len(fails)} failure(s)")
sys.exit(1 if fails else 0)
