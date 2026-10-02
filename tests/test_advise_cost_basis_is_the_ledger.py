"""advise.ranked prices each arm from the LEDGER (money of record), not the calls corpus — the A3 cost basis.

The bug this closes: advise/best-value ranked on the calls corpus, whose per-call cost for a lane-routed call is a
metered-EQUIVALENT estimate — measured ~57x the ledger (one model: ~$1,019 corpus vs ~$18 billed/month). Ranking on
that is ranking on a fiction. A3: COST comes from the ledger (metered arms = real billed $), floored to the amortized
plan-draw (out_tok x lane_eff) for a $0-lane arm; the corpus stays the QUALITY + token source only.

Proved by seeding a DELIBERATE divergence: the corpus cost is set far from the ledger, and ranked must follow the
LEDGER. Offline, isolated SPENDGUARD_HOME, zero spend.
"""
import os
import sys
import tempfile

os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="sg-costbasis-")
os.environ.setdefault("SPENDGUARD_TEST_ISOLATED", "1")
os.environ.setdefault("SPENDGUARD_NO_AUTOINSTALL", "1")

from spendguard import advise, budget, calls, route_economics  # noqa: E402


class Checks:
    def __init__(self):
        self.fails = []

    def __call__(self, label, cond, extra=""):
        if not cond:
            self.fails.append(label)
        print(f"  [{'OK' if cond else 'FAIL'}] {label}{('  — ' + extra) if extra and not cond else ''}")


ck = Checks()
INTENT = "econ-costbasis"

# ── METERED arm: real billed $5.00 in the LEDGER, but a $50 FICTION in the calls corpus. ranked must use $5.00. ───────
budget.record_charge("openai", "gpt-5.5", "realtime", 5.00, intent=INTENT, basis=budget.BASIS_BILLED)
calls.insert("openai", "gpt-5.5", "realtime", 50.00, out_tok=100_000, intent=INTENT, quality="good", quality_conf=1.0, who="test")

# ── LANE arm: NO ledger row (rode a $0 lane → billed $0 → record_charge skips it), a $30 FICTION in the corpus, and
#    50,000 out_tok. With a patched lane_eff of $15/Mtok for anthropic, ranked must price it at 50000*15e-6 = $0.75. ──
calls.insert("anthropic", "claude-opus-4-8", "realtime", 30.00, out_tok=50_000, intent=INTENT, quality="good", quality_conf=1.0, who="test")

_orig_eff = route_economics.lane_eff_by_provider
route_economics.lane_eff_by_provider = lambda: {"anthropic": 15e-6}   # $15 / Mtok amortized plan-draw
try:
    r = advise.ranked(INTENT)
finally:
    route_economics.lane_eff_by_provider = _orig_eff

rows = {m["id"]: m for m in r["models"]}

# ── the LEDGER is the cost basis (metered), not the corpus fiction ──────────────────────────────────────────────────
ck("the metered arm is priced from the LEDGER ($5.00), not the corpus fiction ($50)",
   abs(rows.get("openai:gpt-5.5", {}).get("cost", 0) - 5.00) < 1e-6, extra=repr(rows.get("openai:gpt-5.5", {}).get("cost")))
ck("budget.billed_by_model is the source and reconciles with the ledger",
   abs(budget.billed_by_model(INTENT).get("openai:gpt-5.5", 0) - 5.00) < 1e-6)

# ── the LANE arm (no ledger row) is priced at its amortized plan-draw, not $0 and not the corpus $30 ─────────────────
lane_cost = rows.get("anthropic:claude-opus-4-8", {}).get("cost", 0)
ck("the lane arm is priced at its amortized plan-draw (50000*$15/Mtok = $0.75)", abs(lane_cost - 0.75) < 1e-6, extra=repr(lane_cost))
ck("the lane arm is NOT a flat $0 (never free)", lane_cost > 0)
ck("the lane arm is NOT the corpus fiction ($30)", abs(lane_cost - 30.0) > 1.0)

# ── the ranking follows the ledger-grounded cost: $/good uses the real numbers ───────────────────────────────────────
ck("per_good for the metered arm uses the ledger cost (5.00 / good)", rows.get("openai:gpt-5.5", {}).get("per_good") is not None
   and abs(rows["openai:gpt-5.5"]["per_good"] - (5.00 / rows["openai:gpt-5.5"]["good_rate"])) < 1e-3 if rows.get("openai:gpt-5.5", {}).get("good_rate") else True)

print(f"\n{'OK' if not ck.fails else 'FAIL'} test_advise_cost_basis_is_the_ledger: {len(ck.fails)} failure(s)")
sys.exit(1 if ck.fails else 0)
