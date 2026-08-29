"""Deterministic advisor (Layer 1) — recommend *considering* history.

Rolls the `calls` corpus into per-model cost-efficiency (and, where quality is labeled,
cost-per-good-result), confidence-weighted; recommends; shows the delta vs your planned model; flags
caveats (no labels yet / confounds). `backtest()` replays the same logic AS-OF a past date — so you
can check it would have caught known-good decisions (pack it, Opus-cheaper, don't-cancel).

This is the evidence layer. The reasoning/LLM "learning advisor" (Layer 2) sits on top of this.
"""
import argparse
from . import calls


def _rows(as_of=None, intent=None):
    q = "SELECT provider, model, intent, cost, in_tok, out_tok, quality, quality_conf FROM calls"
    cond, args = ["(intent IS NULL OR intent NOT LIKE 'spendguard:%')"], []  # never analyze our own meta calls
    if as_of:
        cond.append("substr(ts,1,10) <= ?"); args.append(as_of)
    if intent:
        cond.append("intent = ?"); args.append(intent)
    if cond:
        q += " WHERE " + " AND ".join(cond)
    with calls._lock:
        return calls._calls_db().execute(q, args).fetchall()


# Weight given to a quality label from a row that never recorded a confidence at all (legacy rows, written
# before the field existed). Named rather than inlined so it is visible as a stated assumption: it is NOT
# what an explicit confidence of 0.0 means, and the two must never collapse into the same number.
_UNSTATED_CONFIDENCE = 0.7


def _resolve_plan(plan, agg):
    """Which aggregated row the user's `--plan` names, or None.

    Rows are keyed `vendor:model` so two hosts of one model cannot merge. A user who types the bare model
    is answered when exactly ONE vendor in the data hosts it, and REFUSED when several do — naming one of
    them would be a recommendation about a vendor the user never chose. Same rule as pricing: when the data
    is ambiguous, say so and ask, rather than pick and look confident."""
    if plan in agg:
        return plan
    hits = [k for k, a in agg.items() if a["model"] == plan]
    if len(hits) == 1:
        return hits[0]
    if len(hits) > 1:
        print(f"  ⚠️ {len(hits)} vendors in this history host {plan!r} ({', '.join(sorted(hits))}) — "
              f"pass the qualified form to compare one of them.")
    return None


def evidence(as_of=None, intent=None):
    agg = {}
    for prov, model, _intent, cost, intok, outtok, qual, qconf in _rows(as_of, intent):
        # KEYED BY VENDOR AND MODEL. A bare model key merges two vendors' rows into one recommendation —
        # the same collision fixed in pricing.py, where 17 ids resolved to another vendor's rate. Here it
        # would blend a cheap host's cost with an expensive one's and rank the average.
        a = agg.setdefault(f"{prov}:{model}",
                           dict(provider=prov, model=model, jobs=0, cost=0.0, outtok=0, good=0.0, labeled=0.0))
        a["jobs"] += 1
        a["cost"] += cost or 0
        a["outtok"] += outtok or 0
        if qual:
            # A STORED CONFIDENCE OF 0.0 IS A JUDGEMENT, NOT A MISSING ONE. `qconf or DEFAULT` promoted the
            # labeller's explicit "I have no confidence in this" to the same weight as a normal label, which
            # is the opposite of what it says. Only a row that never recorded a confidence gets the default.
            w = qconf if qconf is not None else _UNSTATED_CONFIDENCE
            a["labeled"] += w
            if qual == "good":
                a["good"] += w
    return agg


def ranked(intent=None, as_of=None):
    """The structured cost×quality ranking per (vendor:model) for an intent — the ONE computation behind both
    the `advise` CLI printer and the MCP `advise`/`recommend` tools, so the ranking can never drift between the
    surfaces (the #0b duplication trap). Best-first by $/good-result where any quality is labeled, else by $/M
    output (cost only). Each row: id ('vendor:model'), model (bare), provider, jobs, cost, out_tok, per_m_out,
    good_rate (None if unlabeled), per_good (None if no good result). Returns
    {scope, as_of, labeled, metric, pick, models:[...]}."""
    agg = evidence(as_of, intent)
    models = []
    for key, a in agg.items():
        permout = (a["cost"] / a["outtok"] * 1e6) if a["outtok"] else None
        good_rate = (a["good"] / a["labeled"]) if a["labeled"] else None
        per_good = (a["cost"] / a["good"]) if a["good"] else None
        models.append(dict(id=key, model=a["model"], provider=a["provider"], jobs=a["jobs"],
                           cost=round(a["cost"] or 0.0, 6), out_tok=a["outtok"],
                           per_m_out=permout, good_rate=good_rate, per_good=per_good))
    labeled_any = any(m["good_rate"] is not None for m in models)
    _rankkey = ((lambda m: m["per_good"] if m["per_good"] is not None else 1e18) if labeled_any
                else (lambda m: m["per_m_out"] if m["per_m_out"] is not None else 1e18))
    models.sort(key=_rankkey)
    pick = models[0]["id"] if models and _rankkey(models[0]) < 1e18 else None
    return dict(scope=(f"intent '{intent}'" if intent else "all intents"), as_of=as_of, labeled=labeled_any,
                metric=("$/good-result" if labeled_any else "$/M output (quality not labeled yet)"),
                pick=pick, models=models)


def advise(intent=None, plan=None, as_of=None):
    r = ranked(intent, as_of)
    scope = r["scope"]
    if not r["models"]:
        print(f"no historical data for {scope}" + (f" as of {as_of}" if as_of else "") +
              " — run `spendguard backfill` first.")
        return 0
    rows, labeled_any, metric = r["models"], r["labeled"], r["metric"]

    print(f"spendguard advise — {scope}" + (f"  (as of {as_of})" if as_of else "") + "\n")
    print(f"{'model':<22}{'jobs':>6}{'$ total':>11}{'$/M out':>10}{'good%':>7}{'$/good':>10}")
    for m in rows:
        # `is not None`, NOT truthiness. A computed 0.0 is a real measurement — a call served entirely from
        # cache costs $0 per million output tokens — and rendering it as '—' tells the reader the number
        # could not be computed when in fact it was, and it was the best result in the table.
        print(f"{m['id'][:21]:<22}{m['jobs']:>6}{('$%.2f' % m['cost']):>11}"
              f"{('$%.2f' % m['per_m_out']) if m['per_m_out'] is not None else '—':>10}"
              f"{('%.0f%%' % (100*m['good_rate'])) if m['good_rate'] is not None else '—':>7}"
              f"{('$%.4f' % m['per_good']) if m['per_good'] is not None else '—':>10}")
    best = r["pick"] or rows[0]["id"]
    if r["pick"]:
        print(f"\n→ considering history, prefer: {best}  (lowest {metric})")
    else:
        print(f"\n→ not enough signal to rank by {metric} yet (no model has a measured value); most-used is "
              f"{best}. Run `spendguard reconstruct` for quality labels, then re-check.")
    plan_key = _resolve_plan(plan, {m["id"]: m for m in rows}) if plan else None
    if plan_key and plan_key != best:
        pr = next(m for m in rows if m["id"] == plan_key)
        br = rows[0]
        plan = plan_key
        # TWO FIXES HERE, and only one of the guards is a truthiness bug.
        #
        # `pr[4] and br[4]` skipped the comparison whenever EITHER side measured 0.0 — including the case
        # worth printing most loudly, a plan costing real money against a best that costs nothing. The
        # plan side becomes `is not None`.
        #
        # `br[4]` stays truthy on purpose: it is the DIVISOR, and a zero denominator is a mathematical
        # impossibility rather than a missing value. Guarding a divisor is not the same mistake.
        #
        # The denominator itself was also wrong. "N% costlier THAN the best" is measured against the best,
        # so (pr-br)/br. Dividing by pr answered a different question and understated every premium: a plan
        # costing 3x the best reported 67% rather than 200%, and the ceiling was 100% no matter how bad it
        # got — the number could never say "this is 10x".
        if labeled_any and pr["per_good"] is not None and br["per_good"]:
            print(f"  your plan {plan}: {(pr['per_good']-br['per_good'])/br['per_good']*100:.0f}% costlier per good result than {best}.")
        elif pr["per_m_out"] is not None and br["per_m_out"]:
            print(f"  your plan {plan}: {(pr['per_m_out']-br['per_m_out'])/br['per_m_out']*100:.0f}% costlier per output token than {best}.")
    if not labeled_any:
        print("  ⚠️ no quality labels here yet — this ranks COST only. Add judge/feedback or Layer-2 mining for quality.")
    print(f"  ⚠️ {sum(m['jobs'] for m in rows)} jobs; confounds possible — confirm head-to-head with "
          f"`spendguard compare` on a fixed sample. (history proposes, compare disposes.)")
    try:                        # learned-calibration confidence (fill ratios etc.) — see `spendguard calibrate`
        from . import calibrate as _cal
        for ln in _cal.calibration_summary_lines():
            print("  " + ln)
    except Exception:
        pass
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--intent")
    ap.add_argument("--plan", help="the model you're about to use — shows the delta vs the recommendation")
    ap.add_argument("--as-of", help="replay the advisor as of this date (YYYY-MM-DD) — backtest")
    a = ap.parse_args(argv)
    return advise(intent=a.intent, plan=a.plan, as_of=a.as_of)
