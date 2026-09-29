#!/usr/bin/env python3
"""LIVE proof of the WHOLE-JOB planner (whole_job.run_jobs) — the caller hands a job SET + a GOAL, and spendguard plans
(batch vs realtime, capability-matched), enforces the budget ESTIMATE-FIRST, executes, and returns results + a receipt.
The caller sets NO metered_only / batch / lanes / model — the whole point (#2, the ergonomic front door over
route_economics + bulk_delegate + the Batch-API legs).

The offline test (tests/test_whole_job.py) already proves the CONTRACT deterministically: the estimate-first budget gate
(budget_exceeded / unpriced_under_budget / missing_intent refusals), batch-outage → realtime fallback, collect surfacing
failures/not-ready/no-batch-id, and durable-persist tracking. The one thing no offline test exercises is the LIVE layer:
that a real job set, handed only (jobs, goal), is PLANNED by route_report's true-$ economics, EXECUTED, and returned as
results + a receipt end to end. This script proves that layer.

Estimate-first, per the spend protocol: with NO --run it does the ZERO-SPEND proofs — plan_jobs(auto) (the per-intent
method + $ estimate via route_report), plan_jobs(urgency=batch) (the planner CAN choose the Batch-API leg), and a
missing-intent refusal (attribution is fail-closed). --run adds the ONE live step: run_jobs executes the realtime group
and returns results + a receipt (this set prices to lane_only = $0 billed, so it rides a subscription lane; a lane miss
falls back to the metered API for pennies).

Run under the gated venv (spendguard doctor == ENFORCING HERE: YES). Nothing hardcoded: --intent / --budget are inputs;
the probe reviews are generated sentiment tasks.
"""
import argparse
import json
import sys

DEFAULT_INTENT = "whole-job-planner-live-proof"
DEFAULT_BUDGET_USD = 0.25             # a hard estimate-first gate for the run; this set prices to $0 (lane_only) well under it
_REVIEWS = [                          # short, cheap sentiment tasks — the whole-job SET the caller hands over
    "I absolutely love it, best purchase this year.",
    "It broke on the second day and support ignored me.",
    "The package arrived on the scheduled date.",
    "Fantastic quality, would buy again.",
    "Overpriced and the manual was useless.",
]
_PROMPT = ("Classify the sentiment (positive, negative, or neutral) of this review. Reply with ONE word only: %r")


def _jobs(intent):
    """The job SET: one intent, N sentiment tasks — {id, intent, prompt}. The caller sets NOTHING about routing."""
    return [{"id": "rev-%d" % i, "intent": intent, "prompt": _PROMPT % r} for i, r in enumerate(_REVIEWS)]


def estimate_only(intent, budget):
    """Phase 1 — the ZERO-SPEND plan + the fail-closed attribution gate. No execution, no spend."""
    from spendguard import whole_job
    jobs = _jobs(intent)
    auto = whole_job.plan_jobs(jobs, {})
    print(json.dumps({"phase": "plan(auto)", "n_jobs": len(jobs), "plan": auto["plan"],
                      "est_usd": auto["est_usd"], "unpriced": auto["unpriced"],
                      "note": "route_report's TRUE-$ method pick per intent-group (lane_only → realtime; $0 = a "
                              "subscription lane serves it). The caller chose no method."}, indent=2))
    batch = whole_job.plan_jobs(jobs, {"urgency": "batch"})
    print(json.dumps({"phase": "plan(urgency=batch)", "methods": [g["method"] for g in batch["plan"]],
                      "est_usd": batch["est_usd"], "note": "urgency flips the method to the Batch-API leg — same "
                      "contract, the planner picks the axis the goal asks for."}, indent=2))
    # a job with NO intent → refused (attribution is the core mission; fail-closed, $0)
    ref = whole_job.run_jobs([{"id": "no-intent", "prompt": "x"}], {})
    print(json.dumps({"phase": "attribution-fail-closed", "refused_code": ref["receipt"]["refused_code"],
                      "ran": ref["receipt"].get("ran", 0), "results": len(ref["results"]),
                      "note": "a job missing its intent is REFUSED before any spend — never silently bucketed."}, indent=2))
    return 0


def _is_label(text):
    """The task's answer set is CLOSED — 'reply with ONE word: positive/negative/neutral'. This validates the output is
    EXACTLY one of those labels (normalising case + surrounding punctuation): an ENUM / format-conformance check, NOT a
    substring match and NOT a sentiment judgement. Exact equality distinguishes a delivered-prompt answer ('positive')
    from BOTH a mis-delivered-task deflection ('Could you clarify what rev-2') AND a contract-violating verbose reply
    ('the sentiment is negative because…') — neither is exactly a label. It does not judge whether the label is the
    CORRECT sentiment (that would need a judge)."""
    return (text or "").strip().lower().strip(".!,;:'\"() ") in {"positive", "negative", "neutral"}


def run_live(intent, budget):
    """Phase 2 — the ONE live step: hand (jobs, goal) to run_jobs; it plans, gates, executes, and returns results +
    receipt. PROVEN = every job ran, nothing refused/lost, AND every answer is EXACTLY an in-contract sentiment label
    (not just non-empty text — a garbage deflection or a verbose reply must fail this)."""
    from spendguard import whole_job
    jobs = _jobs(intent)
    r = whole_job.run_jobs(jobs, {"budget_usd": budget, "urgency": "realtime"})
    rec = r["receipt"]
    results = r["results"]
    labelled = {k: _is_label(v.get("text")) for k, v in results.items()}
    proven = (rec.get("refused_code") is None and len(results) == len(jobs)
              and all((v.get("text") and not v.get("error")) for v in results.values())
              and all(labelled.values())                          # every answer is EXACTLY a valid label, not garbage/prose
              and not rec.get("persist_failures") and not rec.get("batch_failures"))
    print(json.dumps({"phase": "run", "plan": r["plan"],
                      "results": {k: (v.get("text") or "").strip()[:40] for k, v in results.items()},
                      "is_valid_label": labelled,
                      "served_by": sorted({(v.get("lane") or v.get("model") or "?") for v in results.values()}),
                      "receipt": {k: rec.get(k) for k in ("est_usd", "groups", "ran", "pending", "batch_failures",
                                                          "persist_failures", "refused_code")},
                      "WHOLE_JOB_PROVEN": proven,
                      "note": ("caller handed (jobs, goal) with NO metered_only/batch/lanes/model — the planner planned, "
                               "gated estimate-first, executed, receipted; every job returned an EXACT in-contract label, "
                               "none lost" if proven else "NOT proven — inspect results/is_valid_label/receipt")}, indent=2))
    return 0 if proven else 4


def main():
    import spendguard
    spendguard.require()                                   # fail closed: halts unless the gate is ENFORCING here
    ap = argparse.ArgumentParser(description="live proof of the whole-job planner (whole_job.run_jobs)")
    ap.add_argument("--intent", default=DEFAULT_INTENT, help="attribution/routing label for the job set")
    ap.add_argument("--budget", type=float, default=DEFAULT_BUDGET_USD, help="estimate-first hard budget for the run")
    ap.add_argument("--run", action="store_true", help="execute the realtime group; omit for the $0 plan/gate proofs")
    a = ap.parse_args()
    return run_live(a.intent, a.budget) if a.run else estimate_only(a.intent, a.budget)


if __name__ == "__main__":
    sys.exit(main())
