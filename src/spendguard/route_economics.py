"""ECONOMIC ROUTING — price a subscription LANE at its TRUE MARGINAL cost and pick the cheapest way to run a bulk
job: the lane, the metered Batch API, or a min-cost COMBO of the two.

Why this exists. best_value ranks by recorded $/good where a subscription-lane call is booked at **$0** — so bulk
work routes onto a plan lane as if free. But a lane token is NOT free: lane_economics already proves an amortized
rate (measured live: claude-code = $200/mo ÷ ~303K-tok session cap ≈ $4.58 / M-tok) AND the capacity is a scarce,
shared budget interactive work needs. "$0 billed" hides both a real rate and a real cap. This module turns the
inputs lane_economics ALREADY computes (eff_usd_per_tok, remaining_abs, used_abs, the pace, the interactive reserve)
+ the Batch-API price into ONE decision: for `n` calls of a given size, what is the true-cost-optimal split?

The economic model (the crux — a PIECEWISE curve, not a flat rate):
  * LANE. The plan fee is SUNK, so consuming capacity that would otherwise EXPIRE UNUSED at reset (the pace says it
    won't be used) is ~$0 — reclaiming waste. Beyond that, each lane token DISPLACES real value and costs
    eff_usd_per_tok (the amortized rate). The interactive RESERVE (a fraction of remaining_abs, or a prompt lane's
    self-use cap) is HELD OUT — bulk never spends it. Past remaining_abs the cap is exhausted; those tokens can't
    ride the lane at all. So the lane's bulk budget is [0 .. remaining_abs − reserve]: the first `free` tokens ~$0,
    the rest at eff, the overflow to batch.
  * BATCH. metered Batch-API price (≈ half realtime, from pricing.batch_cost) × the job's tokens — cap-free, latency
    acceptable for non-interactive bulk.
  * COMBO. the true MIN-COST split: the `free` tokens ride the lane at $0; every other token goes wherever it is
    cheaper per token (lane at eff vs batch), and anything past the lane's bulk budget always overflows to batch.

This layer is COST ARITHMETIC on MEASURED inputs (eff_usd_per_tok, pricing, caps, pace) — never a meaning decision,
so arithmetic is correct here; the QUALITY bar stays an LLM judgement elsewhere (best_value / the advisor). It NEVER
hardcodes a price (pricing.py is the source) and degrades HONESTLY: a lane whose cap is not yet converged is not
priced (no invented rate), and the recommendation falls to batch, said out loud in `why`.
"""
import datetime

from . import pricing


def _days_until(reset_ts, now):
    """Days from `now` to `reset_ts` (an epoch float/int or an ISO string), or None if unparseable — so a missing/bad
    reset can't fabricate a pace (the caller then credits NO free capacity, the conservative direction)."""
    if reset_ts is None:
        return None
    try:
        if isinstance(reset_ts, (int, float)):
            reset = datetime.datetime.fromtimestamp(float(reset_ts), datetime.timezone.utc)
        else:
            reset = datetime.datetime.fromisoformat(str(reset_ts).replace("Z", "+00:00"))
            if reset.tzinfo is None:
                reset = reset.replace(tzinfo=datetime.timezone.utc)
        return max(0.0, (reset - now).total_seconds() / 86400.0)
    except Exception:
        return None


def _free_tokens(binding, now):
    """The lane tokens that would EXPIRE UNUSED at reset at the CURRENT pace — genuinely ~free to consume (the fee is
    sunk and nothing else would use them). = remaining_abs − (pace × days_remaining), pace = used_abs ÷ elapsed.
    0 (conservative) when the pace can't be derived (no reset_ts / period_days) — never over-credit free capacity."""
    R = binding.get("remaining_abs")
    period = binding.get("period_days")
    if R is None or R <= 0 or not period:
        return 0.0
    days_left = _days_until(binding.get("reset_ts"), now)
    if days_left is None:
        return 0.0
    elapsed = max(float(period) - days_left, 1e-6)                # days into the window (>=tiny, so pace is finite)
    pace = (float(binding.get("used_abs") or 0.0) / elapsed)      # tokens/day consumed so far
    projected_more = pace * days_left                            # tokens the pace will consume before reset
    return max(0.0, float(R) - projected_more)                  # the rest would expire unused → ~free


def _lane_geometry(binding, reserve_frac, now):
    """(eff, free_tokens, reserve_tokens, lane_budget) for a converged binding bucket. Bulk may use
    [0 .. lane_budget = remaining_abs − reserve]; the first `free_tokens` are ~$0 (would expire), the rest cost eff.
    The reserve is HELD OUT (interactive). free is clamped into the bulk budget."""
    R = float(binding.get("remaining_abs") or 0.0)
    eff = float(binding.get("eff_usd_per_tok") or 0.0)
    reserve_tokens = max(0.0, float(reserve_frac) * R)
    lane_budget = max(0.0, R - reserve_tokens)
    free = min(_free_tokens(binding, now), lane_budget)
    return eff, free, reserve_tokens, lane_budget


def _batch_cost(model, n_tasks, in_tok, out_tok):
    """Metered Batch-API cost for `n_tasks` calls of (in_tok, out_tok) — pricing.batch_cost (≈ half realtime),
    cap-free. None when the model has no canonical price (a $0-lane id, or an unpriced model) — the caller cannot
    use it as the batch leg then, and says so, never invents a number."""
    try:
        per = pricing.batch_cost(model, int(in_tok), int(out_tok))
    except Exception:
        return None
    if per is None:
        return None
    return float(per) * max(0, int(n_tasks))


def route_cost(intent, n_tasks, in_tok, out_tok, *, lane_binding=None, lane_label=None, batch_model=None,
               reserve_frac=0.2, converged=True, now=None):
    """The TRUE-COST-optimal routing split for `n_tasks` calls of (in_tok, out_tok) each, across a subscription LANE
    (priced piecewise at its true MARGINAL cost), the metered BATCH API, and a min-cost COMBO. Pure arithmetic on
    MEASURED inputs; $0; never hardcodes a price. Returns {intent, n_tasks, tokens, lane_only, batch_only, combo,
    recommend, why}. `lane_binding` is a lane_economics.economics() BINDING bucket (eff_usd_per_tok, remaining_abs,
    used_abs, period_days, reset_ts); `batch_model` is the metered model for the batch leg. Degrades honestly:
    converged=False (cap still estimating) → the lane leg is NOT priced, and the recommendation falls to batch."""
    now = now or datetime.datetime.now(datetime.timezone.utc)
    n = max(0, int(n_tasks))
    in_tok, out_tok = max(0, int(in_tok)), max(0, int(out_tok))
    tok_per_task = in_tok + out_tok
    V = float(n * tok_per_task)
    out = {"intent": intent, "n_tasks": n, "tokens": {"in_per_task": in_tok, "out_per_task": out_tok, "total": int(V)},
           "lane_only": None, "batch_only": None, "combo": None, "recommend": None, "why": None}

    # BATCH leg — per-token rate (pricing.batch_cost ≈ half realtime), or None if the model has no canonical price.
    batch_job = _batch_cost(batch_model, n, in_tok, out_tok) if batch_model else None
    batch_per_tok = (batch_job / V) if (batch_job is not None and V > 0) else None
    if batch_job is not None:
        out["batch_only"] = {"path": "batch", "model": batch_model, "usd": round(batch_job, 6), "tokens": int(V),
                             "usd_per_mtok": round((batch_per_tok or 0.0) * 1e6, 4), "cap_free": True}

    lane_ok = bool(lane_binding) and converged and (lane_binding.get("eff_usd_per_tok") is not None)
    eff = free = reserve_tokens = lane_budget = 0.0
    if lane_ok:
        eff, free, reserve_tokens, lane_budget = _lane_geometry(lane_binding, reserve_frac, now)

        # LANE-ONLY — ALL V on the lane (free tier then eff tier), feasible only if the bulk budget absorbs it.
        lo_free = min(V, free)
        lo_eff = min(V - lo_free, lane_budget - free)
        lo_feasible = (lo_free + lo_eff) >= V - 1e-6
        out["lane_only"] = {"path": "lane", "lane": lane_label, "eff_usd_per_tok": eff,
                            "usd": round(lo_eff * eff, 6) if lo_feasible else None, "feasible": lo_feasible,
                            "free_tokens": round(free, 1), "eff_tokens": round(lo_eff, 1),
                            "reserve_tokens": round(reserve_tokens, 1),
                            "overflow_tokens": round(max(0.0, V - lo_free - lo_eff), 1)}

        # COMBO — TRUE min-cost split: free tokens ride the lane ($0); the rest go wherever is cheaper per token
        # (lane eff vs batch); anything past the bulk budget overflows to batch (cap-free).
        if batch_per_tok is not None:
            c_free = min(V, free)
            rem = V - c_free
            eff_capacity = max(0.0, lane_budget - free)
            c_laneeff = min(rem, eff_capacity) if eff < batch_per_tok else 0.0   # use lane-eff ONLY if it undercuts batch
            c_batch = rem - c_laneeff
            c_cost = c_laneeff * eff + c_batch * batch_per_tok
            out["combo"] = {"path": "combo", "lane": lane_label, "batch_model": batch_model, "usd": round(c_cost, 6),
                            "lane_free_tokens": round(c_free, 1), "lane_eff_tokens": round(c_laneeff, 1),
                            "batch_tokens": round(c_batch, 1), "lane_usd": round(c_laneeff * eff, 6),
                            "batch_usd": round(c_batch * batch_per_tok, 6)}

    # RECOMMEND — the cheapest FEASIBLE path (lane_only only competes when it fits within the cap+reserve).
    options = []
    if out["lane_only"] and out["lane_only"]["feasible"] and out["lane_only"]["usd"] is not None:
        options.append(("lane_only", out["lane_only"]["usd"]))
    if out["combo"] and out["combo"]["usd"] is not None:
        options.append(("combo", out["combo"]["usd"]))
    if out["batch_only"] and out["batch_only"]["usd"] is not None:
        options.append(("batch_only", out["batch_only"]["usd"]))
    if not options:
        out["why"] = "no priced routing option (no batch model and no converged lane) — nothing to compare."
        return out
    pick, pick_usd = min(options, key=lambda kv: kv[1])
    out["recommend"] = {"path": pick, "usd": round(pick_usd, 6)}
    eff_mtok, batch_mtok = eff * 1e6, (batch_per_tok or 0.0) * 1e6
    if not lane_ok:
        out["why"] = ("lane cap not converged yet — priced batch-only (%s @ $%.4f/Mtok); run more on this intent to "
                      "measure the lane rate." % (batch_model, batch_mtok))
    elif pick == "lane_only":
        out["why"] = ("the lane absorbs all %d tokens (%d ~free) for $%.4f — the free/eff budget beats batch." %
                      (int(V), int(free), pick_usd))
    elif pick == "batch_only":
        out["why"] = ("batch $%.4f/Mtok beats the lane's marginal eff $%.2f/Mtok (little/no waste to reclaim), cap-free."
                      % (batch_mtok, eff_mtok))
    else:
        out["why"] = ("combo: %d ~free lane tokens at $0 + %d tokens to batch @ $%.4f/Mtok — cheaper than lane-only or "
                      "batch-only." % (int(out["combo"]["lane_free_tokens"]), int(out["combo"]["batch_tokens"]), batch_mtok))
    return out


def lane_eff_by_provider(now=None):
    """{provider: eff_usd_per_tok} for every CONVERGED subscription lane — the amortized true rate (plan fee prorated
    ÷ measured cap) advise.ranked floors a lane arm's cost with, so best-value / the bandit STOP ranking a plan lane
    as a flat $0. This is the STEADY-STATE rate (what the lane sustainably costs); the one-time WASTE reclaim
    (capacity that would expire unused) is a per-JOB optimisation route_economics handles, never a change to the
    sustained ranking. Empty on any error (fail-open: the ranking then keeps its prior $0 lane, never crashes)."""
    out = {}
    try:
        from . import lane_economics
        for e in lane_economics.economics():
            b = e.get("binding") if e.get("converged") else None
            if b and b.get("eff_usd_per_tok") is not None and e.get("provider"):
                out[e["provider"]] = float(b["eff_usd_per_tok"])
    except Exception:
        pass
    return out


def lane_marginal_usd_per_tok(lane, now=None):
    """The MARGINAL true cost of the NEXT token on `lane` right now: ~$0 while there is WASTE (capacity that would
    expire unused — free at the margin, so interactive work still prefers it), else the amortized eff_usd_per_tok
    (the cap is binding). None when the lane has no converged economics (not priced, never invented). The per-token
    axis a caller consults for a live routing choice, distinct from the steady-state ranking rate above."""
    try:
        import datetime
        from . import lane_economics
        now = now or datetime.datetime.now(datetime.timezone.utc)
        row = next((e for e in lane_economics.economics() if e.get("lane") == lane and e.get("converged")), None)
        b = (row or {}).get("binding")
        if not b or b.get("eff_usd_per_tok") is None:
            return None
        return 0.0 if _free_tokens(b, now) > 0 else float(b["eff_usd_per_tok"])
    except Exception:
        return None


def route_report(intent, n_tasks, *, in_tok=None, out_tok=None, lane=None, batch_model=None, reserve_frac=None, now=None):
    """Resolve the LIVE inputs for `intent` at volume `n_tasks` and run route_economics, returning its result plus a
    `resolved` block that NAMES every input + its basis (so the number is auditable and 'estimating' is visible).
    Resolution, each honest about where it came from: the candidate LANE (the converged economics binding; `lane`
    forces one), the BATCH model (`batch_model`, else config advisor.batch_model), the per-task token sizes (in_tok/
    out_tok, else out estimated via expected_output.expect(sig=intent) and in defaulted to out), and the reserve
    fraction (config advisor.route_reserve_frac, default 0.2). $0 — no call is made, only measured state is read."""
    from . import lane_economics, config, expected_output
    resolved = {"basis": {}}

    bm = batch_model or config._cfg_get("advisor", "batch_model", None)
    resolved["batch_model"] = bm
    resolved["basis"]["batch_model"] = "arg" if batch_model else ("config advisor.batch_model" if bm else "UNSET")

    try:
        econ = lane_economics.economics()
    except Exception:
        econ = []
    if lane:
        row = next((e for e in econ if e.get("lane") == lane), None)
    else:
        row = next((e for e in econ if e.get("converged") and e.get("binding")), None)
    binding = (row or {}).get("binding")
    converged = bool(row and row.get("converged") and binding and binding.get("eff_usd_per_tok") is not None)
    resolved["lane"] = (row or {}).get("lane")
    resolved["basis"]["lane"] = ("arg" if lane else "first converged lane") if row else "no converged lane"
    resolved["lane_converged"] = converged
    if binding and binding.get("eff_usd_per_tok") is not None:
        resolved["lane_eff_usd_per_mtok"] = round(binding["eff_usd_per_tok"] * 1e6, 4)
        resolved["lane_remaining_abs"] = binding.get("remaining_abs")

    if out_tok is None:
        est, basis = expected_output.expect(bm or (row or {}).get("lane") or "", sig=intent)
        out_tok = int(est or 0)
        resolved["basis"]["out_tok"] = "expected_output:" + str(basis)
    else:
        resolved["basis"]["out_tok"] = "arg"
    if in_tok is None:
        in_tok = int(out_tok)                                  # a neutral default when the caller gives no input size
        resolved["basis"]["in_tok"] = "= out_tok (default; pass --in for the real prompt size)"
    else:
        resolved["basis"]["in_tok"] = "arg"

    rf = float(reserve_frac if reserve_frac is not None else config._cfg_get("advisor", "route_reserve_frac", 0.2) or 0.2)
    resolved["reserve_frac"] = rf

    res = route_cost(intent, n_tasks, in_tok, out_tok, lane_binding=binding, lane_label=(row or {}).get("lane"),
                     batch_model=bm, reserve_frac=rf, converged=converged, now=now)
    res["resolved"] = resolved
    return res


def _fmt_usd(x):
    return "—" if x is None else ("$%.4f" % x)


def cmd(argv):
    """CLI: spendguard route-cost <intent> --n N [--in TOK] [--out TOK] [--lane LANE] [--batch-model MODEL] [--json].
    Prints the batch-vs-lane-vs-combo $ table + the recommendation for running N calls of the intent. $0, read-only."""
    import argparse
    import json as _json
    ap = argparse.ArgumentParser(prog="spendguard route-cost")
    ap.add_argument("intent", help="the job-type label the calls are tagged under")
    ap.add_argument("--n", type=int, required=True, help="number of calls (tasks) in the job")
    ap.add_argument("--in", dest="in_tok", type=int, default=None, help="input tokens per call (default: = --out)")
    ap.add_argument("--out", dest="out_tok", type=int, default=None, help="output tokens per call (default: measured p90)")
    ap.add_argument("--lane", default=None, help="force a lane (default: the first converged one)")
    ap.add_argument("--batch-model", dest="batch_model", default=None, help="metered model for the batch leg (default: config advisor.batch_model)")
    ap.add_argument("--json", action="store_true", help="emit the raw result as JSON")
    a = ap.parse_args(argv)
    r = route_report(a.intent, a.n, in_tok=a.in_tok, out_tok=a.out_tok, lane=a.lane, batch_model=a.batch_model)
    if a.json:
        print(_json.dumps(r, indent=2, default=str))
        return 0
    tk = r["tokens"]
    print("\nroute-cost — intent %r · %d call(s) · %d in + %d out tok/call = %s tok total\n"
          % (r["intent"], r["n_tasks"], tk["in_per_task"], tk["out_per_task"], format(tk["total"], ",")))
    rv = r["resolved"]
    _lane_note = ((", eff $%.2f/Mtok, %s tok left" % (rv.get("lane_eff_usd_per_mtok") or 0,
                  format(int(rv.get("lane_remaining_abs") or 0), ","))) if rv.get("lane_converged") else ", NOT converged")
    print("  resolved: lane=%s (%s%s) · batch_model=%s (%s) · reserve=%.0f%%"
          % (rv.get("lane"), rv["basis"].get("lane"), _lane_note, rv.get("batch_model"),
             rv["basis"].get("batch_model"), (rv.get("reserve_frac") or 0) * 100))
    print("\n  %-11s %-12s %s" % ("PATH", "COST", "detail"))
    lo = r["lane_only"]
    if lo:
        _c = _fmt_usd(lo["usd"]) if lo["feasible"] else "infeasible"
        _d = ("eff $%.2f/Mtok · %s free + %s eff tok" % ((lo["eff_usd_per_tok"] or 0) * 1e6,
              format(int(lo["free_tokens"]), ","), format(int(lo["eff_tokens"]), ",")))
        if not lo["feasible"]:
            _d += " · %s tok over the cap+reserve" % format(int(lo["overflow_tokens"]), ",")
        print("  %-11s %-12s %s" % ("lane_only", _c, _d))
    bo = r["batch_only"]
    if bo:
        print("  %-11s %-12s %s @ $%.4f/Mtok · cap-free" % ("batch_only", _fmt_usd(bo["usd"]), bo["model"], bo["usd_per_mtok"]))
    co = r["combo"]
    if co:
        print("  %-11s %-12s %s free lane tok + %s tok to batch" % ("combo", _fmt_usd(co["usd"]),
              format(int(co["lane_free_tokens"]), ","), format(int(co["batch_tokens"]), ",")))
    rec = r["recommend"]
    if rec:
        print("\n  → RECOMMEND: %s at %s" % (rec["path"], _fmt_usd(rec["usd"])))
    print("    %s\n" % r["why"])
    return 0
