"""The deep test: replay REAL call shapes across all four vendors, and measure predicted vs actual.

Not a smoke test. The point is a per-class error figure for the estimator, from your own distribution rather
than published rates — so `spendguard`'s pre-spend number can be trusted for the shapes you actually run.

WHAT IT DOES
  1. Stratifies your `calls` history into call-CLASSES (by caller) and takes each class's measured p50 input
     and p50 output. Those are the shapes that actually occur, not invented ones.
  2. Replays the top classes against every vendor through vendor_call — typed results, bounded deadlines,
     every outcome in the call log with this run's id.
  3. Pairs PREDICTED (expected_output + pricing, zero-spend) against ACTUAL (the vendor's own usage) and
     reports the error per class and per vendor.
  4. Reads the BATCH side from history for free — batch estimate-vs-billed is already recorded.

SAFETY
  --budget is a HARD stop, checked before every call: the run ends rather than exceeding it.
  --estimate is a separate zero-spend pass. Results are appended as they happen, so a partial run is data.
"""
import argparse, json, os, sqlite3, statistics, sys, time

import spendguard                                   # noqa: F401
spendguard.require()
from spendguard import config, pricing, vendor_call as vc, expected_output   # noqa: E402

PANEL = [("anthropic", "claude-opus-4-8"), ("openai", "gpt-5.5"),
         ("moonshot", "kimi-k3"), ("zai", "glm-5.2")]
DEADLINE_S = 180
FILLER = "def handle(rec):\n    x = rec.get('a')\n    return {'k': x, 'n': len(str(x))}\n\n"


def classes(limit):
    """Real call-classes from history: (caller, n, p50_in, p50_out). Stratified by caller, which is the
    closest thing to 'kind of work' the ledger records."""
    c = sqlite3.connect(config.db_path())
    rows = c.execute("SELECT caller, in_tok, out_tok FROM calls WHERE in_tok > 0 AND out_tok > 0 "
                     "AND caller IS NOT NULL").fetchall()
    by = {}
    for caller, i, o in rows:
        by.setdefault(caller.split(":")[0], []).append((i, o))
    out = []
    for name, pts in by.items():
        if len(pts) < 5:
            continue
        out.append({"cls": name, "n": len(pts),
                    "p50_in": int(statistics.median(p[0] for p in pts)),
                    "p50_out": int(statistics.median(p[1] for p in pts))})
    return sorted(out, key=lambda r: -r["n"])[:limit]


def prompt_of(in_tok):
    """A prompt of the MEASURED size. We are calibrating cost and output behaviour, not answer quality, so
    the shape is what has to be faithful — and a synthesized body keeps real data out of a test corpus."""
    body = FILLER * max(1, (in_tok * 4) // len(FILLER))
    return ("Review this code and list every correctness issue, one per line, with a line number.\n\n"
            + body)[: in_tok * 4]


def batch_history():
    """The BATCH side, free: estimate-vs-billed is already in the ledger."""
    c = sqlite3.connect(config.db_path())
    rows = c.execute("SELECT model, SUM(cost), COUNT(*) FROM charges WHERE kind='batch' "
                     "AND (conv_id IS NULL OR conv_id NOT IN ('(true-down)','(impossible-estimate)','(unpriced)')) "
                     "AND day >= date('now','-30 day') GROUP BY model ORDER BY 2 DESC").fetchall()
    corr = c.execute("SELECT COALESCE(SUM(cost),0) FROM charges WHERE conv_id='(true-down)' "
                     "AND day >= date('now','-30 day')").fetchone()[0] or 0
    return rows, corr


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--budget", type=float, default=12.0, help="HARD stop in $")
    ap.add_argument("--classes", type=int, default=5)
    ap.add_argument("--estimate", action="store_true")
    a = ap.parse_args()

    cls = classes(a.classes)
    if not cls:
        print("no call history with token counts — nothing to calibrate against")
        return 1
    print(f"{len(cls)} call-classes from history (stratified by caller):")
    for r in cls:
        print(f"  {r['cls']:<38} n={r['n']:>5}  p50 in={r['p50_in']:>7,}  p50 out={r['p50_out']:>6,}")

    if a.estimate:
        print(f"\nZERO-SPEND ESTIMATE — {len(cls)} classes x {len(PANEL)} vendors = {len(cls)*len(PANEL)} calls")
        tot = 0.0
        for v, m in PANEL:
            sub = sum(pricing.realtime_cost(m, r["p50_in"], r["p50_out"]) for r in cls)
            tot += sub
            print(f"  {v:<10} {m:<18} ${sub:,.3f}")
        print(f"  {'TOTAL':<30} ${tot:,.3f}   (hard budget ${a.budget:,.2f})")
        rows, corr = batch_history()
        print(f"\nBATCH side, from history (free): {len(rows)} models, "
              f"${sum(r[1] for r in rows):,.2f} recorded, ${abs(corr):,.2f} netted by true-down")
        return 0

    out_path = os.path.join(str(config.HOME), "estimator_calibration.jsonl")
    spent = 0.0
    print(f"\nreplaying — hard budget ${a.budget:,.2f}, results append to {out_path}\n")
    for r in cls:
        p = prompt_of(r["p50_in"])
        for v, m in PANEL:
            pred = pricing.realtime_cost(m, r["p50_in"], r["p50_out"])
            if spent + pred > a.budget:
                print(f"  BUDGET STOP: ${spent:,.3f} spent, next call ~${pred:,.3f} > ${a.budget:,.2f}")
                return 0
            cap, basis = vc.output_cap(v, m)
            res = vc.call(v, m, p, deadline_s=DEADLINE_S, purpose=f"calib:{r['cls']}",
                          max_tokens=cap or max(4096, r["p50_out"] * 3))
            act = res.cost if res.cost is not None else 0.0
            spent += act
            err = ((act - pred) / pred * 100) if pred else 0.0
            rec = {"cls": r["cls"], "vendor": v, "model": m, "kind": res.kind,
                   "pred_usd": round(pred, 6), "actual_usd": round(act, 6), "err_pct": round(err, 1),
                   "pred_out": r["p50_out"], "actual_out": res.out_tok, "cap": cap, "cap_basis": basis,
                   "latency": round(res.latency, 1), "run_id": res.run_id}
            with open(out_path, "a") as fh:
                fh.write(json.dumps(rec) + "\n")
            print(f"  {r['cls'][:26]:<28}{v:<10}{res.kind:<16}pred ${pred:>7.4f} actual ${act:>7.4f} "
                  f"({err:>+6.0f}%)  out {res.out_tok:>6,}/{r['p50_out']:<6,} {res.latency:>5.1f}s  "
                  f"[spent ${spent:.2f}]", flush=True)
    print(f"\nDONE — ${spent:,.3f} of ${a.budget:,.2f}. rows -> {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
