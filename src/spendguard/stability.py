"""Measure a model/lane's run-to-run VARIANCE at PRODUCTION settings — the stability question temperature=0 cannot
answer.

THE TWO QUESTIONS ARE DIFFERENT (and only one is a determinism problem):
  • "which arm is better" — a priced A/B. Answered at temperature=0 / seed (adapters.call measurement=True), ONE pass
    per arm, no run noise. Determinism removes the between-run term so a single pass is decisive.
  • "is the shipped screen STABLE" — must be measured at the configuration that SHIPS: the $0 subscription lane, at the
    plan CLI's own default sampling (which spendguard cannot set and the CLIs do not publish — so a claimed lane
    temperature would be a guess). Determinism cannot answer it; the honest characterization is the MEASURED run-to-run
    spread across replicates. This module runs those replicates and reports the spread.

By construction this measures PRODUCTION: it sets no determinism knobs and does NOT pass measurement=True, so each call
rides its normal path (the $0 lane where available) at production sampling. $0 billed on a $0 lane; records nothing
(pure measurement). The replicate count k is the caller's — more replicates tighten the estimate of the spread.
"""
from . import adapters

DEFAULT_REPLICATES = 5          # enough to SEE a spread; the caller raises it to tighten the estimate


def measure_stability(prompts, model, k=DEFAULT_REPLICATES, intent=None, system=None, parse=None, reasoning=None):
    """Run each prompt `k` times on its normal production path and report run-to-run variance.

    `parse` (str -> hashable) extracts the comparable OUTCOME of each reply — a classification label, a decision — which
    is PARSING a fixed-shape result, not judging meaning (so it stays on the right side of the agentic-decision rule);
    the default compares the stripped raw text. For free-text where equivalence is a judgement, the caller passes a
    `parse` that canonicalises, or scores the returned outcomes with its own agentic judge.

    Returns a dict:
      model, k, n_prompts, executor (the path that served — a lane name or 'api', from the first served call)
      per_prompt: [{prompt_idx, outcomes:[o_1..o_k], distinct, stable (1 outcome & 0 errors), errors}]
      stable_frac: prompts stable across all k runs / n_prompts  (1.0 == fully reproducible at production settings)
      flipped:     prompt indices whose outcome was NOT identical across the k runs
      label_rate_by_run: {run_idx: {label: count}} — the per-RUN outcome distribution
      rate_spread: {label: {min_frac, max_frac, band}} — min..max of the label's per-run rate across runs; `band` is the
                   nondeterminism amplitude (the 7.7%↔31.8% = 0.246 band the operator measured) to compare against the
                   between-arm effect an A/B is trying to detect.
    Never raises on a call failure — a failed replicate is counted (errors) and surfaced, never silently dropped."""
    prompts = list(prompts)
    n = len(prompts)
    runs = []                                            # runs[run][prompt] = (outcome, error, executor)
    for _run in range(int(k)):
        row = []
        for p in prompts:
            r = adapters.call(model, p, intent=intent, system=system, reasoning=reasoning)
            if r.get("error") or r.get("text") is None:
                row.append((None, r.get("error") or "empty-reply", r.get("executor")))
                continue
            txt = r["text"].strip()
            try:
                outcome = parse(txt) if parse else txt
            except Exception as e:                       # a parse failure is an OUTCOME error, not a crash
                outcome, = (None,)
                row.append((None, f"parse-error: {str(e)[:80]}", r.get("executor")))
                continue
            row.append((outcome, None, r.get("executor")))
        runs.append(row)

    per_prompt, flipped = [], []
    for pi in range(n):
        outs = [runs[run][pi][0] for run in range(len(runs))]
        errs = sum(1 for run in range(len(runs)) if runs[run][pi][1])
        distinct = len({o for o in outs if o is not None})
        stable = distinct <= 1 and errs == 0
        if not stable:
            flipped.append(pi)
        per_prompt.append({"prompt_idx": pi, "outcomes": outs, "distinct": distinct, "stable": stable, "errors": errs})

    label_rate_by_run = {}
    for run in range(len(runs)):
        dist = {}
        for pi in range(n):
            o = runs[run][pi][0]
            if o is not None:
                dist[o] = dist.get(o, 0) + 1
        label_rate_by_run[run] = dist

    labels = set().union(*[set(d) for d in label_rate_by_run.values()]) if label_rate_by_run else set()
    rate_spread = {}
    for lab in labels:
        fracs = [label_rate_by_run[run].get(lab, 0) / n for run in range(len(runs))] if n else []
        if fracs:
            rate_spread[lab] = {"min_frac": min(fracs), "max_frac": max(fracs), "band": max(fracs) - min(fracs)}

    executor = next((runs[run][pi][2] for run in range(len(runs)) for pi in range(n) if runs[run][pi][2]), None)
    stable_frac = (sum(1 for pp in per_prompt if pp["stable"]) / n) if n else None
    return {"model": model, "k": int(k), "n_prompts": n, "executor": executor,
            "per_prompt": per_prompt, "stable_frac": stable_frac, "flipped": flipped,
            "label_rate_by_run": label_rate_by_run, "rate_spread": rate_spread}


def cmd(argv=None):
    """CLI: `spendguard stability <intent|->  --model M [--k K] [--prompts-file F] [--marker STR]`. Measures the
    model's run-to-run variance at PRODUCTION settings over the intent's recorded prompts (or prompts read one-per-line
    from --prompts-file, or stdin when intent is '-'). `--marker` names a fixed decision prefix to PARSE each reply's
    outcome by (e.g. 'DECISION:') so the stability is scored on the decision, not incidental wording — parsing a fixed
    shape, not judging meaning. This spends only subscription-plan usage ($0 billed) on the $0 lane; prints JSON."""
    import argparse
    import json
    import sys as _sys
    ap = argparse.ArgumentParser(prog="spendguard stability",
                                 description="Measure a model/lane's run-to-run variance at PRODUCTION settings (replicates).")
    ap.add_argument("intent", help="the job-type whose recorded prompts to replay, or '-' to read prompts from stdin")
    ap.add_argument("--model", required=True, help="the model/lane to measure (e.g. gpt-6-luna)")
    ap.add_argument("--k", type=int, default=DEFAULT_REPLICATES, help=f"replicates per prompt (default {DEFAULT_REPLICATES})")
    ap.add_argument("--sample", type=int, default=5, help="how many recorded prompts to replay when sampling the intent (default 5)")
    ap.add_argument("--prompts-file", help="read prompts one-per-line from this file instead of sampling the intent")
    ap.add_argument("--marker", help="parse each reply's OUTCOME as the text after this marker (e.g. 'DECISION:') — "
                    "scores stability on the decision label, not incidental wording")
    ap.add_argument("--system", help="optional system prompt sent with every replicate")
    a = ap.parse_args(argv)
    if a.intent == "-" or a.prompts_file:
        src = open(a.prompts_file) if a.prompts_file else _sys.stdin
        prompts = [ln.rstrip("\n") for ln in src if ln.strip()]
        if a.prompts_file:
            src.close()
    else:
        from . import bakeoff as _bk
        prompts = _bk._sample_prompts(a.intent, a.sample) if hasattr(_bk, "_sample_prompts") else []
    if not prompts:
        print(json.dumps({"error": "no prompts — pass --prompts-file, pipe prompts with intent '-', or use an intent "
                          "with recorded prompts"}), file=_sys.stderr)
        return 1
    marker = a.marker

    def _parse(txt):
        return txt.split(marker, 1)[1].strip() if (marker and marker in txt) else txt.strip()
    r = measure_stability(prompts, a.model, k=a.k, intent=(None if a.intent == "-" else a.intent),
                          system=a.system, parse=(_parse if marker else None))
    print(json.dumps(r, indent=1, default=str))
    return 0
