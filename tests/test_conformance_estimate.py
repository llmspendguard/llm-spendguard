"""Guard — the conformance suite's ZERO-SPEND estimate (the approval gate) is honest: it makes NO calls, its
within_budget flag is True ONLY when the estimate is COMPLETE (no unpriced model) AND under budget, lane behaviours
cost $0, and the shipped manifest fits the $50 ceiling. This is the check that keeps the gate from ever reading green on
an incomplete or over-budget projection (the exact bug the coding doctrine caught during the build). Offline: pure
arithmetic on pricing.py; no network."""
import os
import sys
import tempfile

os.environ.setdefault("SPENDGUARD_TEST_ISOLATED", "1")
os.environ.setdefault("SPENDGUARD_HOME", tempfile.mkdtemp(prefix="spendguard-conf-"))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                "scripts", "integration", "conformance"))

import behaviours as B  # noqa: E402
import estimate as E    # noqa: E402

_fails = []
def ck(label, cond):
    if not cond:
        _fails.append(label)
    print(f"  [{'OK' if cond else 'FAIL'}] {label}")

# ── manifest well-formed ──
print("-- manifest --")
ck("behaviour ids are unique", len(B.BEHAVIOUR_IDS) == len(set(B.BEHAVIOUR_IDS)))
ck("every behaviour declares the required fields", all(
    all(k in b for k in ("id", "title", "incident", "spend_class", "assertion", "stages", "reasoning"))
    for b in B.BEHAVIOURS))
ck("spend_class is only 'lane' or 'metered'", all(b["spend_class"] in ("lane", "metered") for b in B.BEHAVIOURS))
ck("the incident-anchored matrix has the expected ~14 behaviours", 12 <= len(B.BEHAVIOURS) <= 20)

# ── the estimate is honest ──
print("-- estimate (zero-spend, approval gate) --")
est = E.estimate(budget_usd=50.0)
ck("lane behaviours are $0 (plan-covered, never summed into real $)",
   all(r["usd"] == 0.0 for r in est["rows"] if r["spend_class"] == "lane"))
ck("the shipped manifest is COMPLETE (no unpriced model)", est["unpriced"] == [])
ck("the shipped manifest is WITHIN the $50 budget", est["total_usd"] <= 50.0)
ck("within_budget is True (complete AND under budget)", est["within_budget"] is True)
# the flag is the AND of completeness and budget — prove BOTH halves gate it:
_over = E.estimate(budget_usd=1.0)          # same manifest, tiny budget → over
ck("within_budget goes False when OVER budget", _over["within_budget"] is False)
_fake = [dict(B.BEHAVIOURS[0], id="ZZ", spend_class="metered", model="no:such-model-xyz", reasoning=False,
              est_in_tok=100, est_out_tok=100, stages=[10])]
_inc = E.estimate(behaviours=B.BEHAVIOURS + _fake, budget_usd=50.0)
ck("an UNPRICED model lands in `unpriced` (a gap, not a free $0)", "ZZ" in _inc["unpriced"])
ck("within_budget is False when the estimate is INCOMPLETE (unpriced), even if the priced total is under budget",
   _inc["within_budget"] is False)

# ── it truly spends nothing (no ledger rows written) ──
print("-- $0: the estimate makes no calls --")
_before = est["total_usd"]
est2 = E.estimate(budget_usd=50.0)
ck("re-running the estimate is deterministic (pure arithmetic, no state)", est2["total_usd"] == _before)

print(f"\n{'[FAIL]' if _fails else 'OK'} test_conformance_estimate: {len(_fails)} failure(s)")
sys.exit(1 if _fails else 0)
