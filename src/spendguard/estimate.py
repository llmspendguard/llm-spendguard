"""Preflight cost estimator — turn a TINY measured sample into a per-run decision.

Zero paid calls. Pure projection from measured tokens × canonical pricing.py.
This is step 3 (ESTIMATE) of the spend protocol: run a small test, measure real
tokens, then project the FULL job across models AND packing factors so you can
choose per run.

Why packing matters (the $149.76-vs-$33 lesson): the per-request prompt PREFIX
(system + instructions) is re-billed on every request. At 1 item/request you pay
that prefix N times; at 30 items/request you pay it N/30 times. Input is the cost
driver, so packing is usually the biggest lever — bigger than model choice.

TWO ways to supply the token model (get these from your tiny test, see runbook):
  A) explicit:    --prefix-tok 350 --in-per-item 60 --out-per-item 40
  B) from sample: --from-sample sample.jsonl   (lines: {"n":items, "in":in_tok, "out":out_tok})
     Needs >=2 rows with DIFFERENT n to separate prefix from per-item (linear fit).

  python scripts/estimate_job.py --items 263000 --prefix-tok 350 --in-per-item 60 \
         --out-per-item 40 --models gpt-5.5,claude-opus-4-8 --packs 1,10,30 --mode batch
  python scripts/estimate_job.py --items 263000 --from-sample test_usage.jsonl \
         --packs 1,30 --cap-dollars 200
"""
import json, math, argparse

from .pricing import price, normalize

# minimum cacheable prefix (tokens) — below this, prompt caching silently does nothing
MIN_CACHE = {"gpt-5.5": 1024, "gpt-5.5-pro": 1024, "gpt-5.4": 1024,
             "claude-opus-4-8": 4096, "claude-sonnet-4-6": 2048, "claude-haiku-4-5": 4096}


def fit_from_sample(path):
    # The sample is the ONLY measured input to a projection that authorizes real spend, so every way it can
    # be unusable is named here rather than surfacing as a ZeroDivisionError three frames down. A traceback
    # from a cost estimator reads as "the tool is broken" and the usual response is to skip the estimate.
    with open(path) as fh:                       # was never closed — the handle leaked on every fit
        rows = [json.loads(l) for l in fh if l.strip()]
    if not rows:
        raise ValueError(f"{path}: no rows — the sample is empty, so there is nothing to project from. "
                         f"Run the tiny test first; an empty sample cannot authorize a run.")
    missing = [i for i, r in enumerate(rows) if not all(k in r for k in ("n", "in", "out"))]
    if missing:
        raise ValueError(f"{path}: row(s) {missing[:5]} missing one of n/in/out — each line must be "
                         f'{{"n": items, "in": in_tok, "out": out_tok}}')
    bad = [i for i, r in enumerate(rows) if not r["n"] or r["n"] <= 0]
    if bad:
        raise ValueError(f"{path}: row(s) {bad[:5]} have n<=0. n is the ITEM COUNT packed into that request "
                         f"and divides the per-item fit; a zero there is a mis-recorded test, not a data point.")
    ns = sorted({r["n"] for r in rows})
    # per-item output = mean(out/n)
    out_per = sum(r["out"] / r["n"] for r in rows) / len(rows)
    if len(ns) >= 2:  # linear fit in = prefix + n*in_per
        import statistics
        xs = [r["n"] for r in rows]; ys = [r["in"] for r in rows]
        mx, my = statistics.mean(xs), statistics.mean(ys)
        denom = sum((x - mx) ** 2 for x in xs) or 1
        in_per = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / denom
        prefix = my - in_per * mx
        return max(0, prefix), max(0, in_per), out_per
    # single n: cannot separate prefix from per-item — treat whole thing as per-item, prefix unknown
    r = rows[0]
    print(f"  WARNING: sample has one packing size (n={r['n']}). Cannot separate prefix from per-item; "
          f"assuming prefix=0. Re-run the test at two pack sizes for an accurate packing projection.")
    return 0.0, r["in"] / r["n"], out_per


def project(model, items, prefix, in_per, out_per, pack, mode, assume_cache):
    if pack is None or pack <= 0:
        raise ValueError(f"pack must be >= 1 (got {pack!r}) — it is the item count per request and divides "
                         f"the request count; 0 divides by zero and a negative one inverts the projection.")
    p = price(model)
    if mode == "batch":
        # NO INVENTED RATE. This read `cached_in * 0.5` — an assumption that a batch cache-read costs half a
        # realtime cache-read, which appears in no price table and is not true across providers (OpenAI's
        # Batch API does not serve prompt-cache hits at all). It is also wrong in the DANGEROUS direction:
        # a discount the provider will not give makes the projection too LOW, and this number's whole job is
        # to authorize a run. If the canonical table states a batch cache rate, use it; otherwise assume no
        # cache discount and bill the prefix at the batch input rate — conservative, and invented from nothing.
        pin, pout = p["batch_in"], p["batch_out"]
        pcache = p.get("batch_cached_in")
        cache_basis = ("canonical batch_cached_in" if pcache is not None else
                       "NO batch cache rate in pricing.py for this model — projected at the full batch "
                       "input rate (conservative; the real cost may be lower)")
        if pcache is None:
            pcache = pin
    else:
        pin, pout = p["in_"], p["out"]
        pcache = p.get("cached_in")
        # UNPRICED IS NOT FREE. This defaulted to 0.0, so a model with no cached_in in the table projected
        # its cached prefix at ZERO — the same rule this codebase states everywhere else, broken here.
        cache_basis = ("canonical cached_in" if pcache is not None else
                       "NO cached_in rate for this model — projected at the full input rate (conservative)")
        if pcache is None:
            pcache = pin
    n_req = math.ceil(items / pack)
    item_in = items * in_per
    out_tok = items * out_per
    if assume_cache and prefix >= MIN_CACHE.get(normalize(model), 1e9) and n_req > 1:
        prefix_cost = (prefix * pin + (n_req - 1) * prefix * pcache) / 1e6   # 1 full write, rest cache-read
    else:
        prefix_cost = n_req * prefix * pin / 1e6
    cost = item_in * pin / 1e6 + prefix_cost + out_tok * pout / 1e6
    total_in = n_req * prefix + item_in
    return dict(n_req=n_req, in_tok=int(total_in), out_tok=int(out_tok), cost=cost,
                # SAY WHICH RATE PAID FOR THE PREFIX. Without this the caller cannot tell a projection that
                # used a real cache rate from one that fell back to the full input rate, and the two differ
                # by the whole point of packing a shared prefix.
                cache_basis=(cache_basis if assume_cache else "cache not assumed"))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--items", type=int, required=True, help="TOTAL items in the full job")
    ap.add_argument("--prefix-tok", type=float, help="shared per-request prompt prefix tokens (system+instructions)")
    ap.add_argument("--in-per-item", type=float, help="input tokens per item (excluding prefix)")
    ap.add_argument("--out-per-item", type=float, help="output tokens per item")
    ap.add_argument("--from-sample", help="JSONL of measured test requests: {n,in,out}")
    ap.add_argument("--models", default="gpt-5.5,claude-opus-4-8")
    ap.add_argument("--packs", default="1,30")
    ap.add_argument("--mode", default="batch", choices=["batch", "realtime"])
    ap.add_argument("--assume-cache", action="store_true", help="credit prompt-cache on the repeated prefix (only if prefix >= model min)")
    ap.add_argument("--cap-dollars", type=float, help="flag any projection above this (sanity bound)")
    ap.add_argument("--label", help="activity/intent — adds the LEARNED estimate from your history "
                                    "(spendguard calibrate) next to this naive projection")
    a = ap.parse_args()

    if a.from_sample:
        prefix, in_per, out_per = fit_from_sample(a.from_sample)
        print(f"# fit from {a.from_sample}: prefix={prefix:.0f} tok, in/item={in_per:.1f}, out/item={out_per:.1f}")
    else:
        if a.prefix_tok is None or a.in_per_item is None or a.out_per_item is None:
            ap.error("supply --from-sample OR all of --prefix-tok/--in-per-item/--out-per-item")
        prefix, in_per, out_per = a.prefix_tok, a.in_per_item, a.out_per_item

    models = [m.strip() for m in a.models.split(",") if m.strip()]
    if not models:
        ap.error("--models is empty")
    try:
        packs = [int(x) for x in a.packs.split(",") if x.strip()]
    except ValueError:
        ap.error(f"--packs must be integers, got {a.packs!r}")
    # Caught HERE, at the argument, rather than as a ZeroDivisionError inside project(): `--packs 0` is a
    # plausible typo for "unpacked" (which is 1), and the crash gave no hint which of the two it was.
    if not packs or any(pk <= 0 for pk in packs):
        ap.error(f"--packs must all be >= 1 (got {a.packs!r}); 1 means one item per request")
    mode = a.mode
    print(f"# {a.items:,} items · prefix {prefix:.0f} tok/req · {in_per:.1f} in + {out_per:.1f} out per item "
          f"· mode={mode}{' · +cache' if a.assume_cache else ''}\n")
    print(f"{'model':<18}{'pack':>5}{'requests':>10}{'in_tok':>15}{'out_tok':>14}{'$cost':>12}")
    best = None
    rows = []
    for m in models:
        for pk in packs:
            r = project(m, a.items, prefix, in_per, out_per, pk, mode, a.assume_cache)
            flag = ""
            if a.cap_dollars and r["cost"] > a.cap_dollars:
                flag = "  <-- OVER CAP"
            if pk == 1:
                flag += "  (1/req: pays prefix x N — pack to cut this)"
            print(f"{m:<18}{pk:>5}{r['n_req']:>10,}{r['in_tok']:>15,}{r['out_tok']:>14,}{r['cost']:>12,.2f}{flag}")
            rows.append((m, pk, r))
            if best is None or r["cost"] < best[2]["cost"]:
                best = (m, pk, r)
    bm, bp, br = best
    print(f"\nCHEAPEST: {bm} @ pack={bp}  ->  ${br['cost']:,.2f}")
    # show the packing win on the cheapest model
    p1 = next((r for (m, pk, r) in rows if m == bm and pk == 1), None)
    if p1 and bp != 1:
        print(f"  (vs {bm} @ pack=1 = ${p1['cost']:,.2f} — packing saves ${p1['cost']-br['cost']:,.2f})")
    if a.cap_dollars and br["cost"] > a.cap_dollars:
        print(f"  *** even the cheapest option exceeds --cap-dollars ${a.cap_dollars}: shrink scope or prompt before running. ***")
    if a.label:                # the LEARNED correction next to the naive projection (zero spend)
        try:
            from . import calibrate
            per_req_in = prefix + bp * in_per
            lr = calibrate.predict_cost(a.label, n=br["n_req"], model=bm, transport=mode,
                                    est_in_tokens=per_req_in, est_out_max=bp * out_per)
            print(f"  learned ({a.label}): ${lr['p50_usd']:,.2f} p50 … ${lr['p90_usd']:,.2f} p90 "
                  f"[basis={lr['basis']}, level={lr['level']}, n_obs={lr['n_obs']}] — history-corrected")
        except Exception as e:
            print(f"  learned ({a.label}): no calibration yet ({e}) — history will sharpen this")
    print("\nNext: pick a row, run that config, then verify with reconcile_openai_spend.py --estimate <$cost>")


if __name__ == "__main__":
    main()
