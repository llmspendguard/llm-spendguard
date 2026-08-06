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

THE THREE EXECUTION PATHS, and which one calibrates what
  api real-time   billed per token, per-REQUEST usage  → what this script replays. The only path whose
                  numbers are directly comparable to what the estimator predicts.
  api batch       billed at the batch rate, per-request usage, async → read from HISTORY (step 4). Months of
                  real estimate-vs-billed pairs already exist; paying to synthesize more would buy a worse
                  corpus than the one we have.
  subscription    claude-code / codex lanes. $0 marginal and excellent for doing WORK, but they report the
                  CLI's SESSION accounting, not the request's — a 66-token prompt came back as in_tok=2 on
                  the anthropic lane and 13,838 on the openai lane. Not wrong; a different instrument. So
                  --route defaults to `api` HERE and nowhere else: advisor.executor is untouched.

WHAT THIS MEASURES, AND WHAT IT DOES NOT
  VALID: output VARIANCE (identical prompt, N calls), cross-model output size for one identical prompt, and
  the estimator's machinery end-to-end (basis precedence, caps, budget reservation, the three paths).
  NOT VALID: grading predicted-vs-actual for a class, because prompt_of() replays the class's SIZE with a
  generic task. Output size follows the WORK, not the byte count: the 66-token `ground_shots` shape has a
  measured p50 output of 71 tokens for its real prompt and produced 400-2,300 for the synthetic one. That
  gap is the substitution, not estimator error, and reporting it as error would be a fabricated finding.
  To calibrate against real work, replay real prompts from the call_io corpus. That is the honest next step.

SAFETY
  --budget is a HARD stop, checked before every call: the run ends rather than exceeding it.
  --estimate is a separate zero-spend pass. Results are appended as they happen, so a partial run is data.
  Observations are recorded under a CALIB_NS-prefixed sig so a synthetic prompt can never become the
  learned distribution for a real call-class.
"""
import argparse, concurrent.futures as cf, json, os, sqlite3, statistics, sys, threading

import spendguard                                   # noqa: F401
spendguard.require()
from spendguard import bulkgate, config, pricing, vendor_call as vc, expected_output   # noqa: E402

PANEL = [("anthropic", "claude-opus-4-8"), ("openai", "gpt-5.5"),
         ("moonshot", "kimi-k3"), ("zai", "glm-5.2")]
DEADLINE_S = 180
CALIB_NS = "spendguard-calibration:"   # keeps synthetic observations out of production call-class history
FILLER = "def handle(rec):\n    x = rec.get('a')\n    return {'k': x, 'n': len(str(x))}\n\n"


def classes(limit, pct=50):
    """Real call-classes from history: (caller, n, p50_in, p50_out). Stratified by caller, which is the
    closest thing to 'kind of work' the ledger records."""
    c = sqlite3.connect(config.db_path())
    rows = c.execute("SELECT caller, in_tok, out_tok FROM calls WHERE in_tok > 0 AND out_tok > 0 "
                     "AND caller IS NOT NULL").fetchall()
    by = {}
    for caller, i, o in rows:
        by.setdefault(caller.split(":")[0], []).append((i, o))
    out = []
    q = max(0.0, min(1.0, pct / 100.0))
    for name, pts in by.items():
        if len(pts) < 5:
            continue
        ins, outs = sorted(p[0] for p in pts), sorted(p[1] for p in pts)
        idx = lambda a: a[min(len(a) - 1, int(len(a) * q))]      # noqa: E731 — the requested percentile
        out.append({"cls": name, "n": len(pts), "pct": pct, "p50_in": idx(ins), "p50_out": idx(outs)})
    return sorted(out, key=lambda r: -r["n"])[:limit]


def prompt_of(in_tok):
    """A prompt of the MEASURED size. We are calibrating cost and output behaviour, not answer quality, so
    the shape is what has to be faithful — and a synthesized body keeps real data out of a test corpus."""
    body = FILLER * max(1, (in_tok * 4) // len(FILLER))
    return ("Review this code and list every correctness issue, one per line, with a line number.\n\n"
            + body)[: in_tok * 4]


def score(pred_out, act_out, pred_usd, act_usd, seeding, unbilled_but_priced):
    """(tok_err_pct, usd_err_pct, gradeable) — the ONE place a cell's error is decided.

    Extracted and pure because the first version made this decision twice: once for the arithmetic and once
    for the display, and only the display honoured `seeding`. The report then averaged 15 seeded guesses and
    announced "median |token error| 767%". A prediction cannot be graded against the observations that
    created it, and a call the API never billed is not the same measurement — both are UNGRADEABLE, which is
    a different statement from "error unknown" and a very different one from "error large"."""
    gradeable = bool(act_out) and not seeding and not unbilled_but_priced
    if not gradeable:
        return None, None, False
    tok = ((act_out - pred_out) / pred_out * 100) if pred_out else None
    usd = ((act_usd - pred_usd) / pred_usd * 100) if (act_usd > 0 and pred_usd) else None
    return tok, usd, True


def batch_history():
    """The BATCH side, free: estimate-vs-billed is already in the ledger."""
    c = sqlite3.connect(config.db_path())
    rows = c.execute("SELECT model, SUM(cost), COUNT(*) FROM charges WHERE kind='batch' "
                     "AND (conv_id IS NULL OR conv_id NOT IN ('(true-down)','(impossible-estimate)','(unpriced)')) "
                     "AND day >= date('now','-30 day') GROUP BY model ORDER BY 2 DESC").fetchall()
    corr = c.execute("SELECT COALESCE(SUM(cost),0) FROM charges WHERE conv_id='(true-down)' "
                     "AND day >= date('now','-30 day')").fetchone()[0] or 0
    return rows, corr


def report(path, pct):
    """What the run actually established. Rows that could not be scored are NAMED, never averaged away:
    a mean over 20 cells of which 6 were unscoreable is a different number wearing the same label."""
    if not os.path.exists(path):
        print(f"no results at {path}")
        return 1
    rows = [json.loads(l) for l in open(path) if l.strip()]
    rows = [r for r in rows if r.get("pct") == pct]
    if not rows:
        print(f"no rows at p{pct}")
        return 1
    def gradeable(r):
        """Recorded intent, not an incidental non-null. A row is gradeable only if its prediction came from
        history that existed BEFORE the row, and the call went through the API."""
        return (not r.get("seeding")) and (not r.get("unbilled_but_priced")) and r.get("tok_err_pct") is not None
    scored = [r for r in rows if gradeable(r)]
    unscored = [r for r in rows if not gradeable(r)]
    print(f"\nCALIBRATION REPORT — p{pct} shapes, {len(rows)} cells, "
          f"{sum(r.get('calls', 0) for r in rows)} calls, ${sum(r.get('cell_usd', 0) for r in rows):,.2f} billed")

    print("\n  VARIANCE — identical input, N calls. cv is the spread the estimator has to bound.")
    print(f"  {'vendor':<11}{'cells':>6}{'calls':>7}{'med cv':>8}{'max cv':>8}{'widest cell (min-max out)':>34}")
    for v in sorted({r["vendor"] for r in rows}):
        vr = [r for r in rows if r["vendor"] == v and r.get("out_cv") is not None]
        if not vr:
            continue
        w = max(vr, key=lambda r: r["out_cv"])
        print(f"  {v:<11}{len(vr):>6}{sum(r.get('calls', 0) for r in vr):>7}"
              f"{statistics.median(r['out_cv'] for r in vr):>8.2f}{max(r['out_cv'] for r in vr):>8.2f}"
              f"{w['cls'][:16] + ' ' + format(w['out_min'], ',') + '-' + format(w['out_max'], ','):>34}")

    print("\n  OUTPUT SIZE by class x vendor (median of N) — the quantity the estimator predicts")
    vendors = sorted({r["vendor"] for r in rows})
    print(f"  {'class':<24}" + "".join(f"{v:>12}" for v in vendors))
    for cls_name in sorted({r["cls"] for r in rows}):
        cells = {r["vendor"]: r for r in rows if r["cls"] == cls_name}
        print(f"  {cls_name[:22]:<24}" + "".join(
            (f"{cells[v]['actual_out']:>12,}" if v in cells and cells[v].get("actual_out") else f"{'—':>12}")
            for v in vendors))

    if scored:
        errs = [abs(r["tok_err_pct"]) for r in scored]
        print(f"\n  SCORED: {len(scored)} cells, median |token error| {statistics.median(errs):.0f}%")
        print("    Read this as the estimator vs THIS harness's synthetic prompt, not vs the named class —")
        print("    prompt_of() replays the class's size with a generic task. See the module docstring.")
    if unscored:
        print(f"\n  NOT SCORED: {len(unscored)} cells — a first observation cannot be graded against itself.")
        for r in unscored[:6]:
            why = ("NOT-API (lane or metering failure)" if r.get("unbilled_but_priced")
                   else ("seeding — no per-(class,model) history yet" if r.get("seeding") else r.get("kind")))
            print(f"    {r['cls'][:22]:<24}{r['vendor']:<11}{why}")
        if len(unscored) > 6:
            print(f"    ... and {len(unscored) - 6} more")
        print("    These now HAVE history. Re-run to score them; that is the point of the seeding pass.")
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--budget", type=float, default=12.0, help="HARD stop in $")
    ap.add_argument("--classes", type=int, default=5)
    ap.add_argument("--pct", type=int, default=50, help="which percentile of each class's shape to replay")
    ap.add_argument("--repeats", type=int, default=1,
                    help="calls per cell. >1 measures VARIANCE — how much output moves across identical "
                         "inputs — which is the thing a single call cannot tell you, and needs no history.")
    ap.add_argument("--per-vendor", type=int, default=3,
                    help="max concurrent calls per vendor. Cells overlap across the 4 providers; this bounds "
                         "how hard any single one is hit.")
    ap.add_argument("--route", choices=("api", "as-configured"), default="api",
                    help="api (default) FORCES the provider API real-time path, the only one whose usage is "
                         "comparable to the estimate. `as-configured` honours advisor.executor and lets the "
                         "subscription lanes serve — useful to observe the lanes, useless for calibration. "
                         "See the module docstring for the three paths.")
    ap.add_argument("--estimate", action="store_true")
    ap.add_argument("--report", action="store_true",
                    help="read the accumulated results and summarize. Zero spend, and the only place the "
                         "run's conclusions are stated — a scrolling table is data, not a finding.")
    a = ap.parse_args()

    if a.route == "api":
        # Set before the first call — adapters._executor() reads this env var per call.
        os.environ["SPENDGUARD_ADVISOR_EXECUTOR"] = "api"
    print(f"route={a.route}" + ("  (subscription lanes bypassed — every call bills, and is therefore comparable)"
                                if a.route == "api" else
                                "  (WARNING: lane-served calls report session accounting, not request usage)"))
    cls = classes(a.classes, pct=a.pct)
    if not cls:
        print("no call history with token counts — nothing to calibrate against")
        return 1
    print(f"{len(cls)} call-classes from history (stratified by caller, at p{a.pct}):")
    for r in cls:
        print(f"  {r['cls']:<38} n={r['n']:>5}  p50 in={r['p50_in']:>7,}  p50 out={r['p50_out']:>6,}")

    if a.estimate:
        print(f"\nZERO-SPEND ESTIMATE — {len(cls)} classes x {len(PANEL)} vendors x {a.repeats} repeats "
              f"= {len(cls)*len(PANEL)*a.repeats} calls")
        tot = 0.0
        for v, m in PANEL:
            sub = sum(pricing.realtime_cost(m, r["p50_in"], r["p50_out"]) for r in cls) * a.repeats
            tot += sub
            print(f"  {v:<10} {m:<18} ${sub:,.3f}")
        print(f"  {'TOTAL':<30} ${tot:,.3f}   (hard budget ${a.budget:,.2f})")
        rows, corr = batch_history()
        print(f"\nBATCH side, from history (free): {len(rows)} models, "
              f"${sum(r[1] for r in rows):,.2f} recorded, ${abs(corr):,.2f} netted by true-down")
        return 0

    out_path = os.path.join(str(config.HOME), "estimator_calibration.jsonl")
    if a.report:
        return report(out_path, a.pct)
    spent = 0.0
    print(f"\nreplaying — hard budget ${a.budget:,.2f}, results append to {out_path}\n")
    print(f"  {'class':<26}{'vendor':<10}{'kind':<14}{'basis':<10}{'pred out':>9}{'actual':>8}"
          f"{'tok err':>9}{'$ err':>8}{'lat':>7}")
    lock = threading.Lock()
    worst = [0.0]        # the most expensive single call seen so far — the reservation floor (see below)
    fh_lock = threading.Lock()
    stop = threading.Event()

    def run_cell(r, v, m):
        """One (class x vendor) cell: `repeats` identical calls. Returns the record, or None if the budget
        stopped it. Runs on a worker thread — every shared mutation is under `lock`."""
        nonlocal spent
        p = prompt_of(r["p50_in"])
        # FIX 2: predict per (CLASS x MODEL), not per class. Output size is a property of the model as much as
        # the work: on the first run the same class over-ran 3.7x on gpt-5.5 and 25.4x on glm-5.2. A class p50
        # measured on OTHER models does not transfer, so the sig carries both.
        # NAMESPACED. These observations come from a SYNTHETIC prompt of the class's measured size, not from
        # the class's real prompt — so they describe this harness's work, not `providers.py`'s. Writing them
        # under the bare class id would put them in the exact bucket a production caller reads, and the
        # estimator would then predict real work from a stand-in. (Checked: 0 collisions existed. The fix is
        # for the next run, not this one.)
        sig = bulkgate.sig(m, template_id=f"{CALIB_NS}{r['cls']}")
        pred_out, out_basis = expected_output.expect(m, sig=sig)
        seeding = out_basis != "learned"              # no per-model history yet → this run CREATES it
        if seeding:
            pred_out = r["p50_out"]                   # a starting guess, explicitly NOT scored below
        pred_usd = pricing.realtime_cost(m, r["p50_in"], pred_out)
        cap, cap_basis = vc.output_cap(v, m)
        outs, costs, kinds, lats = [], [], [], []
        for _rep in range(max(1, a.repeats)):
            with lock:
                # RESERVE before spending, so N threads cannot each pass the check and collectively overrun.
                # Reserve max(prediction, worst call seen) — reserving the PREDICTION alone is not a bound:
                # this run predicted 194 output tokens for a cell that generated 14,872, and the stop let it
                # through because the reservation was two orders of magnitude too small. Measured overrun on
                # the first run: $12.238 against $12.00. The floor ratchets up as the run learns what calls
                # actually cost, so the residual is one call at the worst observed rate, not unbounded.
                reserve = max(pred_usd, worst[0])
                if stop.is_set() or spent + reserve > a.budget:
                    stop.set()
                    break
                spent += reserve
            res = vc.call(v, m, p, deadline_s=DEADLINE_S, purpose=f"calib:{r['cls']}",
                          max_tokens=cap or max(4096, r["p50_out"] * 3))
            real = res.cost or 0.0
            with lock:
                spent += real - reserve               # settle the reservation against what was actually billed
                worst[0] = max(worst[0], real)
            kinds.append(res.kind)
            lats.append(res.latency)
            costs.append(real)
            if res.ok and res.out_tok:
                outs.append(res.out_tok)
                bulkgate.note_response(sig, m, res.out_tok, cap, res.stop_reason)   # feeds the NEXT run
        if not kinds:
            return None
        # N calls, N answers. The cell's actual is the MEDIAN of them — taking the last (as a single-call probe
        # does) would report one draw from the distribution this run exists to measure.
        act_out = int(statistics.median(outs)) if outs else 0
        act_usd = statistics.median(costs) if costs else 0.0
        # FIX 1: TOKENS are the calibration quantity. Every call reports them — including the ones served by a
        # subscription lane at $0, which the first run scored as -100% cost error and thereby mixed the two axes
        # inside the tool built to keep them apart. Dollars are derived, and only where money was actually billed.
        # A model with a published rate that generated tokens and cost $0 did NOT come from the API. It is a
        # lane, or a metering failure — and either way the row cannot score a $ prediction. Naming it beats
        # showing a plausible percentage. (Same invariant as unpriced != $0.)
        priced = bool(pricing.realtime_cost(m, 1000, 1000))
        billed = act_usd > 0
        unbilled_but_priced = priced and bool(outs) and not billed
        # A SEEDED prediction cannot be scored against the very calls that created it — grading a guess
        # against its own first observation measures nothing and produces a large, authoritative-looking
        # number. `seeding` gates the arithmetic, not just the display; the first version of this gated only
        # the display and the report then averaged 15 seeded guesses into "median error 767%".
        # Tokens from a non-API path are not the same measurement either, so they do not score.
        tok_err, usd_err, _gradeable = score(pred_out, act_out, pred_usd, act_usd, seeding,
                                             unbilled_but_priced)
        mean_out = statistics.mean(outs) if outs else 0
        return {"cls": r["cls"], "vendor": v, "model": m, "kind": kinds[-1], "kinds": kinds, "sig": sig,
                "pred_out": pred_out, "actual_out": act_out, "out_basis": out_basis,
                "seeding": seeding, "tok_err_pct": (round(tok_err, 1) if tok_err is not None else None),
                "billed": billed, "unbilled_but_priced": unbilled_but_priced, "pred_usd": round(pred_usd, 6), "actual_usd": round(act_usd, 6),
                "cell_usd": round(sum(costs), 6),
                "usd_err_pct": (round(usd_err, 1) if usd_err is not None else None),
                "cap": cap, "cap_basis": cap_basis, "latency": round(statistics.median(lats), 1),
                "calls": len(kinds), "ok": len(outs), "outs": outs, "pct": a.pct,
                # VARIANCE: identical input, N calls. A single call cannot distinguish a model that is steady
                # from one that swings 3x, and bounding the second is the estimator's whole job.
                "out_cv": (round(statistics.pstdev(outs) / mean_out, 3) if len(outs) > 1 and mean_out else None),
                "out_min": (min(outs) if outs else None), "out_max": (max(outs) if outs else None)}

    # Cells run concurrently, but each VENDOR is limited to `--per-vendor` in flight — the parallelism is there
    # to make 4 providers overlap, not to rate-limit-storm any one of them.
    gates = {v: threading.Semaphore(a.per_vendor) for v, _ in PANEL}

    def guarded(r, v, m):
        with gates[v]:
            return run_cell(r, v, m)

    cells = [(r, v, m) for r in cls for v, m in PANEL]
    with cf.ThreadPoolExecutor(max_workers=a.per_vendor * len(PANEL)) as pool:
        futs = {pool.submit(guarded, r, v, m): (r, v, m) for r, v, m in cells}
        for fut in cf.as_completed(futs):
            rec = fut.result()
            if rec is None:
                continue
            with fh_lock:
                with open(out_path, "a") as fh:
                    fh.write(json.dumps(rec) + "\n")
            te = ("  seed  " if rec["seeding"] and not rec["unbilled_but_priced"] else
                  (f"{rec['tok_err_pct']:>+7.0f}%" if rec["tok_err_pct"] is not None else "    —  "))
            ue = (f"{rec['usd_err_pct']:>+6.0f}%" if rec["usd_err_pct"] is not None
                  else ("NOT-API" if rec["unbilled_but_priced"] else "   —  "))
            cv = rec["out_cv"]
            rng = f" {rec['out_min']:,}-{rec['out_max']:,}" if cv is not None else ""
            cvs = f" cv={cv:.2f}{rng}" if cv is not None else ""
            print(f"  {rec['cls'][:24]:<26}{rec['vendor']:<10}{rec['kind']:<14}{rec['out_basis']:<10}"
                  f"{rec['pred_out']:>9,}{rec['actual_out']:>8,}{te}{ue}{rec['latency']:>6.1f}s{cvs}", flush=True)
    if stop.is_set():
        print(f"  BUDGET STOP at ${spent:,.3f} of ${a.budget:,.2f} — remaining cells not run")
    over = spent - a.budget
    print(f"\nDONE — ${spent:,.3f} of ${a.budget:,.2f}. rows -> {out_path}")
    if over > 0:
        # Named, never rounded away. A pre-call check cannot un-bill a call already made, so the residual is
        # real money and belongs in the output rather than in a silent rounding.
        print(f"  OVERRAN by ${over:,.3f} ({over / a.budget * 100:.1f}%) — a call billed more than its "
              f"reservation. The stop is a PRE-call check; it cannot unbill a completed request.")
    print("  cells marked `seed` had no per-(class,model) history and now DO — a second run scores them.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
