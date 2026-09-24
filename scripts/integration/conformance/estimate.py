"""ZERO-SPEND estimate for the conformance/soak suite — the approval gate. Pure arithmetic on the behaviour manifest ×
pricing.py × staged sizes: NO model call, $0. Lane ('$0 subscription') behaviours cost nothing; metered behaviours are
priced per-call (reasoning-INCLUSIVE est_out) × the sum of their stages (each stage is a fresh sample, so summing is the
conservative upper bound). Produces the per-behaviour breakdown + total vs the budget, so a human approves the $ BEFORE
any live run. This is the estimate half of the repo's estimate→test→run discipline (bulkgate.gated_batch), applied to
the suite itself."""
import sys

try:
    from .behaviours import BEHAVIOURS
except ImportError:                                  # allow running as a plain script (python estimate.py)
    import os
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from behaviours import BEHAVIOURS


def _bare(model):
    return model.split(":", 1)[-1] if isinstance(model, str) and ":" in model else model


def estimate(behaviours=None, budget_usd=50.0):
    """Return {rows, total_usd, lane_calls, metered_calls, budget_usd, within_budget, unpriced}. Never raises; a model
    with no price-table entry is reported in `unpriced` (its $ is None, NOT silently 0 — an unpriced call is a GAP in the
    estimate, not a free one). CRITICAL: `within_budget` is True ONLY when the estimate is COMPLETE (no unpriced rows)
    AND the total is under budget — an incomplete estimate can never certify the approval gate as green."""
    from spendguard import pricing
    behaviours = behaviours if behaviours is not None else BEHAVIOURS
    rows, total, lane_calls, metered_calls, unpriced = [], 0.0, 0, 0, []
    for b in behaviours:
        calls = sum(b["stages"])
        if b["spend_class"] == "lane":
            lane_calls += calls
            rows.append({"id": b["id"], "title": b["title"], "spend_class": "lane", "model": b["model"],
                         "calls": calls, "per_call_usd": 0.0, "usd": 0.0, "reasoning": b["reasoning"],
                         "tier": b["tier"], "max_spend_usd": None})
            continue
        metered_calls += calls
        try:
            per_call = pricing.realtime_cost(_bare(b["model"]), b["est_in_tok"], b["est_out_tok"])
        except Exception:
            per_call = None
        if per_call is None:
            unpriced.append(b["id"])
            usd = None
        else:
            usd = per_call * calls
            # SELF-CAPPING behaviours (guardrail D's budget_usd, the refusal halt, the small bounded fans) will not
            # spend stages×per_call — they stop at their own ceiling. max_spend_usd is that conservative bound, so the
            # projection is the REAL upper bound, not a stages figure the run can never reach.
            if b.get("max_spend_usd") is not None:
                usd = min(usd, float(b["max_spend_usd"]))
            total += usd
        rows.append({"id": b["id"], "title": b["title"], "spend_class": "metered", "model": b["model"],
                     "calls": calls, "in_tok": b["est_in_tok"], "out_tok": b["est_out_tok"],
                     "per_call_usd": per_call, "usd": usd, "reasoning": b["reasoning"],
                     "tier": b["tier"], "max_spend_usd": b.get("max_spend_usd")})
    # within_budget requires a COMPLETE estimate: an unpriced row means the true total is UNKNOWN, so the gate is
    # indeterminate and must NOT read as green (this is the approval gate — a false 'within' would authorise a run
    # whose cost we could not bound).
    within = (not unpriced) and (total <= budget_usd)
    return {"rows": rows, "total_usd": round(total, 2), "lane_calls": lane_calls, "metered_calls": metered_calls,
            "budget_usd": budget_usd, "within_budget": within, "unpriced": unpriced}


_TIER_NAME = {0: "TIER 0 — $0 safety rails (free)", 1: "TIER 1 — the $ BACKSTOPS (cheap, highest value)",
              2: "TIER 2 — pacing & failover", 3: "TIER 3 — reasoning-economics soak (priciest; last)"}


def render(est):
    """Human-readable projection for `--estimate`, grouped by TIER (the hard-stop run order) with a running cumulative
    so the reader sees exactly what a budget cut would and would not sacrifice."""
    out = ["Conformance soak — ZERO-SPEND estimate (no calls made), in HARD-STOP RUN ORDER:", ""]
    cum = 0.0
    for tier in (0, 1, 2, 3):
        trows = [r for r in est["rows"] if r.get("tier") == tier]
        if not trows:
            continue
        out.append("  " + _TIER_NAME[tier])
        out.append("  %-4s %-40s %-7s %7s %12s %10s" % ("id", "behaviour", "spend", "calls", "per-call $", "total $"))
        tier_sum = 0.0
        for r in trows:
            pc = "—" if r["per_call_usd"] is None else ("$%.5f" % r["per_call_usd"])
            tot = "—(unpriced)" if r["usd"] is None else ("$0" if r["usd"] == 0 else "$%.3f" % r["usd"])
            cap = (" (self-caps ≤$%.2f)" % r["max_spend_usd"]) if r.get("max_spend_usd") else ""
            out.append("  %-4s %-40s %-7s %7d %12s %10s%s"
                       % (r["id"], r["title"][:40], r["spend_class"], r["calls"], pc, tot, cap))
            tier_sum += (r["usd"] or 0.0)
        cum += tier_sum
        out.append("      tier subtotal: $%.2f   ·   cumulative through tier %d: $%.2f" % (tier_sum, tier, cum))
        out.append("")
    out.append("  metered calls: %d   ·   lane ($0) calls: %d" % (est["metered_calls"], est["lane_calls"]))
    out.append("  PROJECTED REAL $ (metered, API only): $%.2f   vs budget $%.2f  →  %s"
               % (est["total_usd"], est["budget_usd"],
                  "WITHIN + complete" if est["within_budget"]
                  else ("OVER budget" if not est["unpriced"] else "INDETERMINATE (unpriced rows)")))
    if est["unpriced"]:
        out.append("  ⚠ UNPRICED (no price-table entry, excluded from the total — RESOLVE before running): %s"
                   % ", ".join(est["unpriced"]))
    out.append("  NOTE: real API $ only; $0 lane usage is plan-covered (est-value, never summed into the real $).")
    return "\n".join(out)


def main(argv=None):
    import spendguard
    spendguard.require()                             # fail closed — the estimate runs UNDER the gate like everything else
    est = estimate()
    print(render(est))
    return 0 if est["within_budget"] else 1          # non-zero on OVER budget OR any unpriced row (gate not green)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
