#!/usr/bin/env python
"""Four-provider correctness review of the lane_queue drain / batch-offload / availability state machine.

WHY: a failure here bills the metered API (the 67% failed-row rate = real money), so Ash asked for a cross-provider
correctness check — four DISTINCT vendors, each reviewing the WHOLE relevant code (never a truncated slice — the bug
could be anywhere), answering the same focused questions, so a defect one model misses another catches.

Runs UNDER the gate. ESTIMATE-FIRST: `--estimate` (default) counts tokens + prints the per-provider and total cost
and SPENDS NOTHING; `--run` executes the four calls. Each reviewer's whole answer is written to
scripts/review/out/ for synthesis. Models are resolved from the catalog (priced); no hardcoded prices.
"""
import os
import sys
import json
import argparse
import pathlib

import spendguard
spendguard.require()
from spendguard import adapters, calls, pricing, config  # noqa: E402

REPO = pathlib.Path(__file__).resolve().parents[2]
SRC = REPO / "src" / "spendguard"

# The code under review — sent WHOLE (no slicing of evidence a judgement reads). These three files are the queue's
# correctness surface: the durable state machine, the batch offload, and the capacity planner.
FILES = ["lane_queue.py", "batch_tracker.py", "queue_planner.py"]

# Four DISTINCT providers (one model each). Resolved against the catalog; each must be priced or the run refuses.
REVIEWERS = {
    "openai": "gpt-5.1",
    "anthropic": "claude-sonnet-4-6",
    "google": "gemini-3.1-pro",
    "zai": "glm-4.6",
}

SYS = (
    "You are a senior distributed-systems engineer reviewing a durable work-queue for CORRECTNESS. The queue stores "
    "LLM tasks as SQLite rows and a periodic 'drain' leases pending rows and runs them on $0 subscription lanes, "
    "falling back to the METERED API on failure (so a bug that mis-handles state BILLS REAL MONEY). Review the code "
    "provided IN FULL and answer precisely, citing function names and the exact lines/conditions. Do not speculate "
    "beyond the code shown; if something needed to judge a point is not in the code, say so explicitly."
)

QUESTIONS = (
    "Answer these, each with VERDICT (correct / bug / cannot-tell from this code) + the specific code evidence:\n"
    "1. BATCH/REALTIME EXCLUSIVITY (double-spend): when a task is offloaded to the Batch API, is it GUARANTEED to "
    "leave the realtime-leasable set so it can never also be run realtime (billed twice)? Trace lease() ↔ "
    "mark_batched() ↔ settle() ↔ collect_batched() ↔ requeue_from_batch(). Can any interleaving, crash point, or "
    "error path leave a row BOTH queued_batch AND leasable, or run it on both paths?\n"
    "2. AVAILABILITY / TIMELINESS: when lanes are saturated (429-risk) or logged out, does the drain defer work on a "
    "real cooldown and drain only when capacity exists, or does it churn (re-lease/park/retry, blocking handshakes) "
    "and burn CPU + fall over to metered? Identify where it wastes work or bills unnecessarily, and whether a task "
    "is delivered in a timely way when capacity returns.\n"
    "3. FAILURE PATH COST: with a 67% failed-row rate observed, trace what a FAILURE does — attempts/parks, the "
    "metered fallback, and whether a transient lane outage is wrongly recorded as a permanent failure. Where does "
    "money leak?\n"
    "4. DURABILITY / LOSS: can a lease expiry, crash, or the single-instance drain lock ever LOSE a row or "
    "double-deliver a result?\n"
    "Be concrete and ranked by severity (money first). If the code is correct on a point, say so plainly."
)


def _bundle():
    parts = []
    for name in FILES:
        body = (SRC / name).read_text()      # WHOLE file — the reviewer must see all of it
        parts.append("===== FILE: src/spendguard/%s (%d lines) =====\n%s" % (name, body.count("\n") + 1, body))
    return "\n\n".join(parts)


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", action="store_true", help="execute the 4 metered calls (default: estimate only, $0)")
    args = ap.parse_args(argv)

    code = _bundle()
    prompt = QUESTIONS + "\n\n" + code
    in_tok = adapters_toklen = None
    try:
        from spendguard import attribution
        in_tok = attribution._toklen(SYS + prompt)
    except Exception:
        in_tok = len(SYS + prompt) // 4
    out_budget = 4000                                   # a thorough structured review

    # per-provider + total estimate (no spend)
    print("4-provider queue-correctness review — input ~%d tok/provider, output budget %d tok\n" % (in_tok, out_budget))
    total = 0.0
    priced = {}
    for prov, model in REVIEWERS.items():
        try:
            est = pricing.realtime_cost(model, in_tok, out_budget)
        except Exception as e:
            est = None
            print("  %-10s %-24s UNPRICED (%s) — will refuse to run" % (prov, model, str(e)[:50]))
            continue
        priced[prov] = (model, est)
        total += est
        print("  %-10s %-24s ~$%.4f" % (prov, model, est))
    print("\n  TOTAL (4 providers): ~$%.4f  [REAL metered $]" % total)

    if len(priced) < len(REVIEWERS):
        print("\nREFUSING: not every reviewer model is priced — fix the catalog before spending.")
        return 2
    if not args.run:
        print("\nestimate only — re-run with --run to execute. Nothing spent.")
        return 0

    outdir = pathlib.Path(__file__).resolve().parent / "out"
    outdir.mkdir(parents=True, exist_ok=True)
    results = {}
    for prov, (model, est) in priced.items():
        print("\n>>> %s (%s) reviewing…" % (prov, model))
        with calls.context(intent="spendguard:queue-correctness-review"):
            r = adapters.call(model, prompt, system=SYS, max_tokens=out_budget, no_substitution=True)
        text = r.get("text") or ""
        err = r.get("error")
        (outdir / ("review_%s.md" % prov)).write_text("# %s (%s)\n\nerror=%s\n\n%s" % (prov, model, err, text))
        results[prov] = {"model": model, "error": err, "chars": len(text)}
        print("    %s — %d chars%s" % (model, len(text), (" ERROR: " + str(err)[:80]) if err else ""))
    (outdir / "_summary.json").write_text(json.dumps(results, indent=2))
    print("\nwrote %d reviews to %s" % (len(results), outdir))
    return 0


if __name__ == "__main__":
    sys.exit(main())
