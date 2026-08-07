"""Replay REAL prompts from the call_io corpus across all four vendors: calibration + reliability.

WHY THIS EXISTS, AND WHY IT IS NOT estimator_calibration_run.py
  That script replays a class's measured SIZE with a synthesized task, so its predicted-vs-actual figures
  describe the stand-in rather than the work — stated plainly in its own docstring. Output size follows the
  WORK. This script replays the ACTUAL prompt text, so predicted-vs-actual is finally a real measurement.

TWO QUESTIONS, ONE RUN
  CALIBRATION — for the model that originally ran a prompt, call_io holds its true output token count. That
    is a ground truth the synthetic harness never had: predicted vs the row's own recorded out_tok.
  RELIABILITY — the same real prompt, N times, on all four vendors. What fraction come back ok / empty /
    truncated / refused / timed out, how far the output moves across identical calls, and how long it takes.
    "Does it work" is a rate over repeats, not one green call.

WHAT THE CORPUS CAN AND CANNOT SUPPORT (read before trusting a number)
  Rows whose prompt was stored at the `callio.snip_chars` cap are CUT, and a cut prompt is a different task.
  They are excluded here, and the count of exclusions is printed rather than quietly dropped.
  Historical rows were captured at the old 800-char cap, so the usable set is the SHORT end of real work
  (tens to a few hundred tokens of input). Refilling them at full length is impossible — the provider input
  files have expired. The cap is now configurable and `callio.record` grows a row rather than ignoring it,
  so future captures are recoverable; this run cannot retroactively become wide.

SAFETY
  Every prompt passes through deid.redact before egress. These are real prompts, not synthesized ones.
  --budget is a HARD stop; reservations floor at the most expensive call seen, so the residual is one call
  at the worst observed rate rather than unbounded. --estimate is a separate zero-spend pass.
"""
import argparse, collections, concurrent.futures as cf, json, os, sqlite3, statistics, sys, threading

import spendguard                                   # noqa: F401
spendguard.require()
from spendguard import bulkgate, callio, config, deid, expected_output, pricing, vendor_call as vc   # noqa: E402

PANEL = [("anthropic", "claude-opus-4-8"), ("openai", "gpt-5.5"),
         ("moonshot", "kimi-k3"), ("zai", "glm-5.2")]
DEADLINE_S = 180
REPLAY_NS = "spendguard-realprompt:"    # keeps replay observations out of production call-class history


def corpus(limit, min_chars=40):
    """Real prompts with an intact body and a true recorded output size.

    A row stored AT the snip cap was cut mid-prompt; replaying it measures a truncated task and would report
    the difference as estimator error. Excluded, and counted out loud — a silently smaller corpus is how a
    narrow result comes to wear a broad label.

    Cut-ness is judged against the cap IN FORCE WHEN THE ROW WAS WRITTEN, not today's. Raising the cap does
    not un-cut an old row, but comparing against the current value makes 265 truncated rows read as complete
    the instant the setting changes — the corpus appears to grow while the data is untouched. Rows carry no
    capture-cap column, so the test is exact equality with the shipped default (every historical row was
    written under it) or at/above the current one."""
    caps = {callio.snip_chars(), callio._IO_SNIP_DEFAULT}
    cur = callio.snip_chars()
    c = sqlite3.connect(config.db_path())
    rows = c.execute(
        "SELECT id, intent, model, prompt, out_tok, COALESCE(system,''), COALESCE(req_schema,''), "
        "COALESCE(req_max_tokens,0) FROM call_io "
        "WHERE prompt IS NOT NULL AND out_tok > 0 AND LENGTH(prompt) >= ? ORDER BY LENGTH(prompt) DESC",
        (min_chars,)).fetchall()
    def is_cut(text):
        n = len(text)
        return n >= cur or n in caps
    cut = [r for r in rows if is_cut(r[3])]
    whole = [r for r in rows if not is_cut(r[3])]
    by = collections.defaultdict(list)               # stratify by intent so one intent cannot dominate
    for r in whole:
        by[r[1] or "(none)"].append(r)
    out, i = [], 0
    while len(out) < limit and any(v for v in by.values()):
        for k in sorted(by):
            if by[k] and len(out) < limit:
                r = by[k].pop(0)
                out.append({"io_id": r[0], "intent": r[1] or "(none)", "origin_model": r[2],
                            "prompt": r[3], "truth_out": int(r[4]),
                            "system": r[5] or "", "req_schema": r[6] or "",
                            "req_max_tokens": int(r[7] or 0)})
        i += 1
        if i > limit:
            break
    return out, len(cut), len(whole)


def batch_history():
    """The BATCH side, free: estimate-vs-billed is already in the ledger."""
    c = sqlite3.connect(config.db_path())
    rows = c.execute("SELECT model, SUM(cost), COUNT(*) FROM charges WHERE kind='batch' "
                     "AND (conv_id IS NULL OR conv_id NOT IN ('(true-down)','(impossible-estimate)','(unpriced)')) "
                     "AND day >= date('now','-30 day') GROUP BY model ORDER BY 2 DESC").fetchall()
    return rows


def score(pred_out, act_out, pred_usd, act_usd, seeding, unbilled_but_priced):
    """(tok_err_pct, usd_err_pct, gradeable) — the ONE place a cell's error is decided.

    Mirrors estimator_calibration_run.score deliberately: a seeded prediction graded against the very calls
    that created it is not a measurement, and a call the API never billed is a different instrument. Both are
    UNGRADEABLE, which is a different statement from "error unknown" and a very different one from "error
    large". Zero output is a failed call, never a prediction that came in pleasingly low."""
    gradeable = bool(act_out) and not seeding and not unbilled_but_priced
    if not gradeable:
        return None, None, False
    tok = ((act_out - pred_out) / pred_out * 100) if pred_out else None
    usd = ((act_usd - pred_usd) / pred_usd * 100) if (act_usd > 0 and pred_usd) else None
    return tok, usd, True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--budget", type=float, default=12.0, help="HARD stop in $")
    ap.add_argument("--prompts", type=int, default=8, help="distinct real prompts to replay")
    ap.add_argument("--repeats", type=int, default=5,
                    help="calls per (prompt, vendor). This is the RELIABILITY sample: one green call cannot "
                         "distinguish 'works' from 'works 80%% of the time'.")
    ap.add_argument("--per-vendor", type=int, default=3, help="max concurrent calls per vendor")
    ap.add_argument("--estimate", action="store_true")
    ap.add_argument("--report", action="store_true", help="summarize accumulated results; zero spend")
    a = ap.parse_args()

    # The API real-time path only. A subscription lane reports the CLI's session accounting, not the
    # request's usage, so its numbers cannot calibrate an API estimator (measured: in_tok=2 for a 66-token
    # prompt). advisor.executor is untouched — this is scoped to the run.
    os.environ["SPENDGUARD_ADVISOR_EXECUTOR"] = "api"
    out_path = os.path.join(str(config.HOME), "realprompt_replay.jsonl")
    if a.report:
        return report(out_path)

    rows, n_cut, n_whole = corpus(a.prompts)
    if not rows:
        print("no complete real prompts in call_io — nothing to replay")
        return 1
    print(f"corpus: {n_whole} complete prompts usable, {n_cut} EXCLUDED as cut at a snip cap "
          f"(default {callio._IO_SNIP_DEFAULT}, current {callio.snip_chars()}) — a cut prompt is a different task")
    print(f"replaying {len(rows)} of them x {len(PANEL)} vendors x {a.repeats} repeats "
          f"= {len(rows) * len(PANEL) * a.repeats} calls\n")
    for r in rows:
        print(f"  {r['intent'][:26]:<28}{r['origin_model'][:16]:<18}prompt {len(r['prompt']):>6,} chars   "
              f"true out {r['truth_out']:>6,} tok")

    if a.estimate:
        print("\nZERO-SPEND ESTIMATE")
        tot = 0.0
        for v, m in PANEL:
            # Estimate the OUTPUT the same way the gate would, so the estimate reflects the shipped estimator
            # rather than a private guess made for this script.
            sub = 0.0
            for r in rows:
                pred, _ = expected_output.expect(m, sig=bulkgate.sig(m, template_id=f"{REPLAY_NS}{r['intent']}"))
                sub += pricing.realtime_cost(m, len(r["prompt"]) // 4, pred or r["truth_out"])
            sub *= a.repeats
            tot += sub
            print(f"  {v:<10} {m:<18} ${sub:,.3f}")
        print(f"  {'TOTAL':<30} ${tot:,.3f}   (hard budget ${a.budget:,.2f})")
        rows_b = batch_history()
        print(f"\nBATCH side, from history (free): {len(rows_b)} models, ${sum(r[1] for r in rows_b):,.2f} recorded")
        return 0

    spent = 0.0
    worst = [0.0]
    lock, fh_lock, stop = threading.Lock(), threading.Lock(), threading.Event()
    print(f"\nreplaying — hard budget ${a.budget:,.2f}, results append to {out_path}\n")
    print(f"  {'intent':<24}{'vendor':<10}{'ok/N':>7}{'kinds':<26}{'med out':>9}{'cv':>6}{'p90 lat':>9}")

    def run_cell(r, v, m):
        nonlocal spent
        # DE-ID BEFORE EGRESS. These are real prompts recovered from real work; the synthetic harness had
        # nothing to protect and this one does. redact() fails open toward privacy and never raises.
        prompt = deid.redact(r["prompt"])
        system = deid.redact(r["system"]) or None
        # THE REQUEST SHAPE, not just the prompt. Sending the question without the system message and the
        # output contract is a different call: measured on this very corpus, prompts whose originals were
        # schema-forced came back 10-32x larger when replayed bare (truth 4 tokens -> 77-133), while the
        # free-text ones landed within 10-31%. Replaying only the prompt and calling the gap "estimator
        # error" would have been a fabricated finding.
        schema = None
        if r["req_schema"]:
            try:
                schema = json.loads(r["req_schema"]) or None
            except Exception:
                schema = None
        faithful = bool(system or schema or r["req_max_tokens"])
        sig = bulkgate.sig(m, template_id=f"{REPLAY_NS}{r['intent']}")
        pred_out, out_basis = expected_output.expect(m, sig=sig)
        seeding = out_basis != "learned"
        if seeding:
            pred_out = r["truth_out"]                # the ORIGINAL model's real output — a real starting point
        pred_usd = pricing.realtime_cost(m, len(prompt) // 4, pred_out)
        cap, cap_basis = vc.output_cap(v, m)
        outs, costs, kinds, lats = [], [], [], []
        for _rep in range(max(1, a.repeats)):
            with lock:
                reserve = max(pred_usd, worst[0])    # reserving the PREDICTION alone is not a bound
                if stop.is_set() or spent + reserve > a.budget:
                    stop.set()
                    break
                spent += reserve
            res = vc.call(v, m, prompt, deadline_s=DEADLINE_S, purpose=f"replay:{r['intent']}",
                          system=system, schema=schema if isinstance(schema, dict) else None,
                          max_tokens=(r["req_max_tokens"] or cap or max(4096, r["truth_out"] * 4)))
            real = res.cost or 0.0
            with lock:
                spent += real - reserve
                worst[0] = max(worst[0], real)
            kinds.append(res.kind)
            lats.append(res.latency)
            costs.append(real)
            if res.ok and res.out_tok:
                outs.append(res.out_tok)
                bulkgate.note_response(sig, m, res.out_tok, cap, res.stop_reason)
        if not kinds:
            return None
        act_out = int(statistics.median(outs)) if outs else 0
        act_usd = statistics.median(costs) if costs else 0.0
        priced = bool(pricing.realtime_cost(m, 1000, 1000))
        unbilled_but_priced = priced and bool(outs) and act_usd <= 0
        # A row with no recorded request shape cannot be replayed faithfully, so it cannot be scored against
        # its own recorded output — that comparison would be between two different calls.
        tok_err, usd_err, gradeable = score(pred_out, act_out, pred_usd, act_usd,
                                            seeding or not faithful, unbilled_but_priced)
        mean_out = statistics.mean(outs) if outs else 0
        return {"io_id": r["io_id"], "intent": r["intent"], "origin_model": r["origin_model"],
                "prompt_chars": len(prompt), "truth_out": r["truth_out"],
                "vendor": v, "model": m, "sig": sig, "is_origin_model": (m == r["origin_model"]),
                "faithful": faithful, "had_system": bool(system), "had_schema": bool(schema),
                "req_max_tokens": r["req_max_tokens"],
                "calls": len(kinds), "ok": len(outs), "kinds": dict(collections.Counter(kinds)),
                "pred_out": pred_out, "out_basis": out_basis, "seeding": seeding,
                "actual_out": act_out, "out_min": (min(outs) if outs else None),
                "out_max": (max(outs) if outs else None),
                "out_cv": (round(statistics.pstdev(outs) / mean_out, 3) if len(outs) > 1 and mean_out else None),
                "tok_err_pct": (round(tok_err, 1) if tok_err is not None else None),
                "usd_err_pct": (round(usd_err, 1) if usd_err is not None else None),
                "gradeable": gradeable, "unbilled_but_priced": unbilled_but_priced,
                "pred_usd": round(pred_usd, 6), "actual_usd": round(act_usd, 6),
                "cell_usd": round(sum(costs), 6), "cap": cap, "cap_basis": cap_basis,
                "lat_p50": round(statistics.median(lats), 1), "lat_p90": round(max(lats), 1),
                "run_id": res.run_id}

    gates = {v: threading.Semaphore(a.per_vendor) for v, _ in PANEL}

    def guarded(r, v, m):
        with gates[v]:
            return run_cell(r, v, m)

    cells = [(r, v, m) for r in rows for v, m in PANEL]
    with cf.ThreadPoolExecutor(max_workers=a.per_vendor * len(PANEL)) as pool:
        futs = [pool.submit(guarded, r, v, m) for r, v, m in cells]
        for fut in cf.as_completed(futs):
            rec = fut.result()
            if rec is None:
                continue
            with fh_lock, open(out_path, "a") as fh:
                fh.write(json.dumps(rec) + "\n")
            ks = " ".join(f"{k}:{n}" for k, n in sorted(rec["kinds"].items()))
            cv = f"{rec['out_cv']:.2f}" if rec["out_cv"] is not None else "—"
            print(f"  {rec['intent'][:22]:<24}{rec['vendor']:<10}{rec['ok']:>3}/{rec['calls']:<3}{ks:<26}"
                  f"{rec['actual_out']:>9,}{cv:>6}{rec['lat_p90']:>8.1f}s", flush=True)
    over = spent - a.budget
    print(f"\nDONE — ${spent:,.3f} of ${a.budget:,.2f}. rows -> {out_path}")
    if over > 0:
        print(f"  OVERRAN by ${over:,.3f} — a pre-call check cannot unbill a completed request.")
    return report(out_path)


def report(path):
    if not os.path.exists(path):
        print(f"no results at {path}")
        return 1
    rows = [json.loads(l) for l in open(path) if l.strip()]
    if not rows:
        print("no rows")
        return 1
    calls = sum(r["calls"] for r in rows)
    print(f"\nREAL-PROMPT REPLAY — {len(rows)} cells, {calls} calls, "
          f"${sum(r['cell_usd'] for r in rows):,.2f} billed")

    print("\n  RELIABILITY — the same real prompt, N times. A rate, not one green call.")
    print(f"  {'vendor':<11}{'calls':>7}{'ok':>8}{'not-ok kinds':<34}{'med cv':>8}{'p90 lat':>9}")
    for v in sorted({r["vendor"] for r in rows}):
        vr = [r for r in rows if r["vendor"] == v]
        n = sum(r["calls"] for r in vr)
        ok = sum(r["ok"] for r in vr)
        bad = collections.Counter()
        for r in vr:
            for k, c in r["kinds"].items():
                if k != "ok":
                    bad[k] += c
        cvs = [r["out_cv"] for r in vr if r["out_cv"] is not None]
        print(f"  {v:<11}{n:>7}{ok / n * 100 if n else 0:>7.0f}%"
              f"{(' '.join(f'{k}:{c}' for k, c in bad.most_common()) or '—'):<34}"
              f"{(statistics.median(cvs) if cvs else 0):>8.2f}"
              f"{max((r['lat_p90'] for r in vr), default=0):>8.1f}s")

    # The calibration the synthetic harness could not do: the model that ACTUALLY ran this prompt has a true
    # recorded output size, so replayed-vs-truth is a like-for-like comparison of the same work.
    origin = [r for r in rows if r["is_origin_model"] and r["ok"] and r["truth_out"] and r.get("faithful")]
    unfaithful = [r for r in rows if r["is_origin_model"] and r["ok"] and not r.get("faithful")]
    if unfaithful:
        print(f"\n  {len(unfaithful)} origin-model cell(s) NOT compared to their recorded output: the row "
              f"stores no system message, output contract, or max_tokens, so the replay is a different call. "
              f"Comparing anyway is how a 1,107% 'estimator error' gets reported when the real finding is "
              f"that a schema-forced 4-token answer was replayed unconstrained. Re-fetch with "
              f"`spendguard callio fetch` to capture the request shape.")
    if origin:
        print("\n  GROUND TRUTH — replayed output vs the size this model really produced for this prompt")
        print(f"  {'intent':<26}{'model':<18}{'truth':>9}{'replayed':>10}{'delta':>9}")
        for r in origin:
            d = (r["actual_out"] - r["truth_out"]) / r["truth_out"] * 100
            print(f"  {r['intent'][:24]:<26}{r['model'][:16]:<18}{r['truth_out']:>9,}"
                  f"{r['actual_out']:>10,}{d:>+8.0f}%")
        ds = [abs((r["actual_out"] - r["truth_out"]) / r["truth_out"] * 100) for r in origin]
        print(f"  median |delta| vs recorded truth: {statistics.median(ds):.0f}%  (n={len(origin)})")
    else:
        print("\n  GROUND TRUTH: no cell ran on the model that originally produced its prompt — "
              "nothing to compare against. Not an error, and not a pass either.")

    graded = [r for r in rows if r.get("gradeable")]
    seeding = [r for r in rows if r.get("seeding")]
    if graded:
        e = [abs(r["tok_err_pct"]) for r in graded if r.get("tok_err_pct") is not None]
        if e:
            print(f"\n  ESTIMATOR, scored cells only: n={len(e)}, median |token error| {statistics.median(e):.0f}%")
    if seeding:
        print(f"\n  {len(seeding)} cells SEEDING — first observation for their (intent, model); a prediction "
              f"cannot be graded against the calls that created it. Re-run to score them.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
