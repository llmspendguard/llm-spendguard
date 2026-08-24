"""spendguard bakeoff — the EXPLORE half of the model-advisor: measure cost×quality for models you have NOT
used on a job-type, so advise/recommend can rank the whole universe, not just your history.

advise/recommend rank from RECORDED evidence, so a model you have never run is invisible to them. A bakeoff
fills that in the only honest way there is — it RUNS a slate of candidate models on a SAMPLE of the intent's
real tasks, JUDGES each output for quality, and RECORDS the cost + good/bad per (intent, model) into the same
`calls` corpus advise reads. After a bakeoff, the untried models have a $/good and fall into the ranking.

Inside spendguard's rails, always: ESTIMATE-FIRST (run=False is a zero-spend projection + the plan; a
`budget_usd` refuses before spending), the fan-out prefers $0 subscription LANES and is metered by the gate
otherwise, and the quality JUDGE is an LLM (never a keyword rule) — the same judge advisor.reconstruct uses.

  bakeoff('code-review', candidates=['openai:gpt-5.5','deepseek:deepseek-v4-flash'], sample_n=5)  # estimate
  bakeoff('code-review', candidates=[...], sample_n=5, run=True, budget_usd=0.50)                 # measure
"""
import sqlite3

from . import calls, config, pricing, callio
from .submit import _count_tokens

_BAKEOFF_OUT_EST = 500     # per-call OUTPUT tokens for the PRE-FLIGHT estimate only — a budget guard, not a cap;
#                            the real call floors output at the model max and bills ACTUAL tokens. Named, not inline.
_JUDGE_OUT = 20            # room for the structured verdict {"good": true|false}; still tiny, keeps the judge cheap
_JUDGE_CONF = 0.9          # confidence stamped on a bakeoff quality label — an LLM judge on a fresh output, not a guess
# The judge is an LLM (agentic decision) that returns a STRUCTURED boolean — never a free-text verdict a string
# rule then interprets. A schema-forced {"good": bool} removes the "GOOD enough … BAD" misread class entirely: the
# model decides, the code reads a typed field, and anything that is not a clean boolean is treated as UNLABELED.
_JUDGE_SYS = ("You evaluate whether an LLM OUTPUT is a usable, CORRECT result for its PROMPT. Return ONLY JSON: "
              '{"good": true} when the output is a usable, correct result; {"good": false} otherwise. No prose.')
_JUDGE_SCHEMA = {"type": "object", "additionalProperties": False, "required": ["good"],
                 "properties": {"good": {"type": "boolean"}}}


def _sample_prompts(intent, n):
    """Up to `n` DISTINCT real prompts recorded for this intent (the tasks to replay on the candidates). Returns
    [] when the intent has no recovered prompts — the caller must then pass prompts explicitly (a bakeoff cannot
    invent representative work)."""
    try:
        con = sqlite3.connect(getattr(callio, "DB_PATH", None) or str(config.HOME / "call_io.sqlite"))
    except Exception:
        return []
    try:
        con.row_factory = sqlite3.Row
        rows = con.execute("SELECT DISTINCT prompt FROM call_io WHERE intent IS ? AND prompt IS NOT NULL "
                           "AND length(prompt) > 0 LIMIT ?", (intent, int(n))).fetchall()
        return [r["prompt"] for r in rows]
    except Exception:
        return []
    finally:
        con.close()


def _judge_one(prompt, output, judge_model):
    """Is this (prompt, output) a good result? Decided by an LLM (the quality signal), returned as a STRUCTURED
    boolean — not a free-text verdict a string rule then interprets. Returns True (good) / False (bad) / None
    (the judge failed or gave no clean boolean → UNLABELED, never guessed)."""
    from . import adapters, advisor
    import json as _json
    r = adapters.call(judge_model, advisor._judge_prompt(prompt[:4000], (output or "")[:4000]),
                      max_tokens=_JUDGE_OUT, system=_JUDGE_SYS, schema=_JUDGE_SCHEMA, sig="spendguard:bakeoff-judge")
    if r.get("error") or not r.get("text"):
        return None
    try:
        j = r.get("json") if isinstance(r.get("json"), dict) else _json.loads(r["text"])
        return bool(j["good"]) if isinstance(j, dict) and isinstance(j.get("good"), bool) else None
    except Exception:
        return None


def _plan(intent, candidates, prompts, judge_model):
    """The zero-spend plan + cost estimate: every candidate runs every sample prompt, then each output is judged.
    Coarse and slightly high on purpose (a budget guard, not the ledger): a per-prompt input count, a fixed
    output estimate. Returns (estimate_usd, per_candidate_detail, n_runs, n_judge)."""
    in_toks = [_count_tokens(p, candidates[0] if candidates else "gpt-5.5") for p in prompts]
    total, detail = 0.0, {}
    for c in candidates:
        c_cost = 0.0
        for it in in_toks:
            try:
                c_cost += pricing.realtime_cost(c, it, _BAKEOFF_OUT_EST)
            except Exception:
                c_cost = None
                break
        detail[c] = c_cost
        total += (c_cost or 0.0)
    n_judge = len(candidates) * len(prompts)                        # one small judge call per (candidate, prompt)
    try:
        judge_in = sum(_count_tokens(p, judge_model) for p in prompts) * len(candidates)
        total += pricing.realtime_cost(judge_model, judge_in, _JUDGE_OUT * n_judge)
    except Exception:
        pass
    return total, detail, len(candidates) * len(prompts), n_judge


def bakeoff(intent, candidates=None, prompts=None, sample_n=5, run=False, budget_usd=None):
    """Measure cost×quality for `candidates` on a SAMPLE of `intent`'s real tasks, judge each output, and (run=True)
    record it so advise/recommend rank the candidates. `candidates` = ['vendor:model', …] (required — the slate to
    test). `prompts` overrides the auto-sample (from the intent's recorded prompts). ESTIMATE-FIRST: run=False
    returns the plan + $ estimate and spends nothing; run=True executes (refusing if the estimate exceeds
    `budget_usd`). Returns a structured dict."""
    judge_model = config.advisor_judge_model()
    candidates = [c.strip() for c in (candidates or []) if c and c.strip()]
    if not candidates:
        return dict(intent=intent, error="no candidates — pass candidates=['vendor:model', …] (the slate to test). "
                    "spendguard_recommend suggests a slate from your priced catalogue.")
    prompts = list(prompts) if prompts else _sample_prompts(intent, sample_n)
    if not prompts:
        return dict(intent=intent, candidates=candidates, error="no sample tasks — this intent has no recorded "
                    "prompts to replay. Pass prompts=[…] with a few representative tasks.")

    est, detail, n_runs, n_judge = _plan(intent, candidates, prompts, judge_model)
    unpriced = [c for c, v in detail.items() if v is None]
    if not run:
        return dict(intent=intent, candidates=candidates, sample=len(prompts), runs=n_runs, judge_calls=n_judge,
                    estimate_usd=round(est, 5), per_candidate=detail, unpriced=unpriced, estimate_only=True,
                    note=f"estimate only (~${est:.4f} + judging); call with run=True to measure. "
                         "$0 subscription lanes are used where available.")
    if budget_usd is not None and est > float(budget_usd):
        return dict(intent=intent, candidates=candidates, estimate_usd=round(est, 5), refused=True,
                    note=f"estimate ~${est:.4f} exceeds budget_usd ${float(budget_usd):.4f} — not run. Raise budget_usd "
                         "or shrink the slate/sample.")

    from . import adapters
    results = {}
    for c in candidates:
        n_good = n_lab = n_run = n_err = 0
        spent, last_error = 0.0, None
        for p in prompts:
            r = adapters.call(c, p, sig=intent, timeout_s=120)      # gated; prefers a $0 lane, meters otherwise
            if r.get("error"):
                n_err += 1                                          # a dropped run is COUNTED + surfaced, never silent —
                last_error = (r.get("error") or "")[:140]          # a candidate that fails every prompt must be visible,
                continue                                           # not read as a clean zero-run bakeoff
            n_run += 1
            cost = float(r.get("cost") or 0.0)
            spent += cost
            verdict = _judge_one(p, r.get("text"), judge_model)     # LLM judge — the quality signal
            q = None if verdict is None else ("good" if verdict else "bad")
            if q is not None:
                n_lab += 1
                n_good += 1 if verdict else 0
            # RECORD into the same corpus advise/recommend read, so the candidate now has a $/good for this intent
            calls.insert(adapters.provider_for(c), c.split(":", 1)[-1], "realtime", cost,
                         in_tok=int(r.get("in_tok") or 0), out_tok=int(r.get("out_tok") or 0),
                         intent=intent, quality=q, quality_conf=(_JUDGE_CONF if q else None), who="bakeoff")
        results[c] = dict(runs=n_run, failed=n_err, last_error=last_error, labeled=n_lab, good=n_good,
                          good_rate=(n_good / n_lab if n_lab else None), spent=round(spent, 6),
                          per_good=(spent / n_good if n_good else None))

    from . import advise
    ranked = advise.ranked(intent=intent)                          # re-rank now that the candidates have evidence
    return dict(intent=intent, sample=len(prompts), judged_by=judge_model, per_candidate=results,
                ranking=ranked["models"], pick=ranked["pick"], ranked_by=ranked["metric"],
                note="recorded to the corpus — spendguard_advise / spendguard_recommend now include these models.")


def main(argv=None):
    import argparse
    import json
    ap = argparse.ArgumentParser(prog="spendguard bakeoff",
                                 description="Measure cost×quality for a slate of models on a sample of an intent's tasks.")
    ap.add_argument("intent")
    ap.add_argument("--candidates", required=True, help="comma-separated vendor:model slate to test")
    ap.add_argument("--sample", type=int, default=5, help="how many recorded prompts to replay (default 5)")
    ap.add_argument("--run", action="store_true", help="actually spend (default: estimate only)")
    ap.add_argument("--budget", type=float, help="refuse if the estimate exceeds this")
    a = ap.parse_args(argv)
    r = bakeoff(a.intent, candidates=[c for c in a.candidates.split(",") if c.strip()],
                sample_n=a.sample, run=a.run, budget_usd=a.budget)
    print(json.dumps(r, indent=1, default=str))
    return 0 if not r.get("error") else 1
