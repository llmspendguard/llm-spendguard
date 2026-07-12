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
        return calls._db().execute(q, args).fetchall()


def evidence(as_of=None, intent=None):
    agg = {}
    for prov, model, _intent, cost, intok, outtok, qual, qconf in _rows(as_of, intent):
        a = agg.setdefault(model, dict(provider=prov, jobs=0, cost=0.0, outtok=0, good=0.0, labeled=0.0))
        a["jobs"] += 1
        a["cost"] += cost or 0
        a["outtok"] += outtok or 0
        if qual:
            w = qconf or 0.7
            a["labeled"] += w
            if qual == "good":
                a["good"] += w
    return agg


def advise(intent=None, plan=None, as_of=None):
    agg = evidence(as_of, intent)
    scope = f"intent '{intent}'" if intent else "all intents"
    if not agg:
        print(f"no historical data for {scope}" + (f" as of {as_of}" if as_of else "") +
              " — run `spendguard backfill` first.")
        return 0
    rows = []
    for model, a in agg.items():
        permout = (a["cost"] / a["outtok"] * 1e6) if a["outtok"] else None
        good_rate = (a["good"] / a["labeled"]) if a["labeled"] else None
        per_good = (a["cost"] / a["good"]) if a["good"] else None
        rows.append([model, a, permout, good_rate, per_good])
    labeled_any = any(r[3] is not None for r in rows)
    key = (lambda r: r[4] if r[4] is not None else 1e18) if labeled_any else (lambda r: r[2] if r[2] is not None else 1e18)
    rows.sort(key=key)

    print(f"spendguard advise — {scope}" + (f"  (as of {as_of})" if as_of else "") + "\n")
    print(f"{'model':<22}{'jobs':>6}{'$ total':>11}{'$/M out':>10}{'good%':>7}{'$/good':>10}")
    for model, a, permout, good_rate, per_good in rows:
        print(f"{model[:21]:<22}{a['jobs']:>6}{('$%.2f' % a['cost']):>11}"
              f"{('$%.2f' % permout) if permout else '—':>10}"
              f"{('%.0f%%' % (100*good_rate)) if good_rate is not None else '—':>7}"
              f"{('$%.4f' % per_good) if per_good else '—':>10}")
    best = rows[0][0]
    metric = "$/good-result" if labeled_any else "$/M output (quality not labeled here yet)"
    print(f"\n→ considering history, prefer: {best}  (lowest {metric})")
    if plan and plan in agg and plan != best:
        pr = next(r for r in rows if r[0] == plan)
        br = rows[0]
        if labeled_any and pr[4] and br[4]:
            print(f"  your plan {plan}: {(pr[4]-br[4])/pr[4]*100:.0f}% costlier per good result than {best}.")
        elif pr[2] and br[2]:
            print(f"  your plan {plan}: {(pr[2]-br[2])/pr[2]*100:.0f}% costlier per output token than {best}.")
    if not labeled_any:
        print("  ⚠️ no quality labels here yet — this ranks COST only. Add judge/feedback or Layer-2 mining for quality.")
    print(f"  ⚠️ {sum(r[1]['jobs'] for r in rows)} jobs; confounds possible — confirm head-to-head with "
          f"`spendguard compare` on a fixed sample. (history proposes, compare disposes.)")
    try:                        # learned-calibration confidence (fill ratios etc.) — see `spendguard calibrate`
        from . import calibrate as _cal
        for ln in _cal.summary_lines():
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
