#!/usr/bin/env python
"""ZERO-SPEND grounding + estimate for the A2.1 value-judge before/after MEASUREMENT (reads the real ledger/calls;
makes NO LLM call). It SURFACES EVIDENCE for a target judgement + prices the run — it does NOT pick the target.

  1. OUTPUT-SPREAD EVIDENCE — per intent, the mean OUTPUT-token spread across its measured arms (hi/lo), shown as a
     DESCRIPTIVE statistic for intents that have ≥2 comparable arms. This is NOT a target verdict: a high spread does
     NOT mean over-reasoning — the longer output may be NECESSARY (a correct, required proof) rather than wasteful. Which
     intent to MEASURE is a JUDGEMENT on the actual recorded work (does the extra output earn its tokens? is the intent
     real work, not a smoke test / backfill?), made by a person or an agentic assessment — never by this spread alone.
     Rows with no out_tok and `suspect` rows (impossible per-call values) are excluded; every considered intent is
     COUNTED and NAMED (no silent skips) so the coverage is honest.
  2. PER-JUDGE-CALL COST — from lane_bandit.estimate_judge_cost (the same priced-prompt arithmetic the bandit uses).
  3. THE ESTIMATE — the before/after A/B is: for M real task pairs, judge each pair in BOTH modes (pure-quality =
     _value_judge_prompt with no costs = the 'before'; value = with realized costs = the 'after'), plus one independent
     quality-grade per pair to confirm the value pick didn't sacrifice quality. So billed calls = M × 3 small judge
     calls (lane OUTPUT generation is $0, plan-served). Priced for a few staged M.

Run UNDER the gate (fail-closed). Nothing here spends; it only prints evidence + what a paid run WOULD cost, for a
target judgement and approval."""
import spendguard
spendguard.require()                                   # fail closed — refuse to run if the gate is not enforcing here

from spendguard import calls, lane_bandit


def _output_spread_evidence(min_calls=4, min_arm_n=2):
    """DESCRIPTIVE output-token spread per intent (hi.mean_out / lo.mean_out) for intents that HAVE ≥2 arms each with
    ≥min_arm_n measured calls — EVIDENCE for a target judgement, NOT a target verdict (a high spread may be necessary
    reasoning, not waste; deciding that is a judgement on the real outputs). Returns (rows_sorted_by_spread_for_display,
    coverage) where coverage NAMES every considered intent and whether it has a comparable pair — no silent drops, so the
    denominator is honest. Sorting is for DISPLAY only; it is not a ranking of 'best targets'."""
    rows = []
    considered = calls.recorded_intents(min_calls=min_calls) or []
    no_comparable_pair = []                            # NAMED, not a silent `continue` — the coverage must be visible
    for it in considered:
        norms = calls.mean_out_by_executor_model(it) or {}   # already excludes suspect/no-out_tok rows
        arms = [(k, v) for k, v in norms.items()
                if (v.get("mean_out") or 0) > 0 and (v.get("n") or 0) >= min_arm_n]
        if len(arms) < 2:
            no_comparable_pair.append(it)
            continue
        ordered = sorted(arms, key=lambda kv: kv[1]["mean_out"])
        lo_k, lo = ordered[0]
        hi_k, hi = ordered[-1]
        spread = hi["mean_out"] / lo["mean_out"] if lo["mean_out"] else 0.0
        rows.append({"intent": it, "spread": spread, "arms": len(arms),
                     "lo": (lo_k, round(lo["mean_out"]), lo["n"]), "hi": (hi_k, round(hi["mean_out"]), hi["n"])})
    rows.sort(key=lambda r: -r["spread"])             # DISPLAY order only (biggest output difference first) — not a verdict
    coverage = {"considered": len(considered), "with_pair": len(rows), "no_comparable_pair": no_comparable_pair}
    return rows, coverage


def main():
    print("== A2.1 value-judge measurement — grounding evidence + ESTIMATE (zero spend) ==\n")

    rows, cov = _output_spread_evidence()
    print("COVERAGE: considered %d recorded intents (≥4 calls); %d have ≥2 comparable arms; %d do not." %
          (cov["considered"], cov["with_pair"], len(cov["no_comparable_pair"])))
    if cov["no_comparable_pair"]:
        _shown = ", ".join(cov["no_comparable_pair"][:15])
        print("  no comparable pair (named): %s%s" %
              (_shown, (" … +%d more" % (len(cov["no_comparable_pair"]) - 15)) if len(cov["no_comparable_pair"]) > 15 else ""))

    print("\nOUTPUT-SPREAD EVIDENCE (descriptive, display-sorted — NOT a target verdict). A high spread is NOT itself")
    print("over-reasoning: the longer output may be NECESSARY. Choosing the target is a JUDGEMENT on the real outputs")
    print("(is the extra output wasteful? is the intent real work, not a smoke test / backfill?):")
    for r in rows[:15]:
        print(f"  {r['spread']:7.0f}x  {r['intent']:<34}  arms={r['arms']}  "
              f"lean={r['lo'][0][0]}:{r['lo'][0][1]}~{r['lo'][1]}tok(n{r['lo'][2]})  "
              f"heavy={r['hi'][0][0]}:{r['hi'][0][1]}~{r['hi'][1]}tok(n{r['hi'][2]})")

    jc = lane_bandit.estimate_judge_cost()
    per = jc.get("per_bakeoff_usd")
    print(f"\nPER-JUDGE-CALL COST (judge model {jc.get('judge_model')}): "
          f"${per if per is not None else '—'}  (in≈{jc.get('in_tok_bound')} tok bound, out cap {jc.get('out_tok_cap')})")

    print("\nESTIMATE — before/after A/B = M real task pairs × 3 small judge calls (pure-mode + value-mode + a quality"
          " grade); lane OUTPUT generation is $0 (plan-served). Staged M (never a large batch):")
    print("   M pairs | billed judge calls | est $ (billed)")
    for M in (10, 15, 20):
        n = 3 * M
        tot = (per * n) if per is not None else None
        print(f"   {M:>7} | {n:>18} | {('$%.4f' % tot) if tot is not None else '—'}")
    print("\n   (lane generation: 2 lane calls/pair = 2M plan-served calls, $0 billed but counts plan usage + time.)")
    print("\nThis is EVIDENCE + an ESTIMATE ONLY — nothing was spent. A target is a JUDGEMENT on the evidence above;")
    print("approve a specific (target intent, M) to RUN it.")


if __name__ == "__main__":
    raise SystemExit(main())
