"""HORIZON ROUTING (item C) — allocate a whole SUBMITTED job set across the subscription LANE, the metered BATCH API,
and the metered REALTIME API over the plan's reset-window horizon, honouring each job's URGENCY and the SHARED, scarce
lane capacity.

route_economics.route_cost prices ONE group against the full lane budget as a snapshot. This layer adds the two things
a whole-SET, horizon-aware plan needs that a single snapshot cannot express:

  1. URGENCY — a caller-declared FACT, never guessed (quality stays the LLM's job; this is time, not meaning). An
     URGENT group must run now, so its lane overflow goes to the metered REALTIME API (fast, real $), NEVER the slow
     Batch API. A DEFERRABLE group can wait, so its lane overflow goes to the cheap, cap-free BATCH API, and the
     ~free/waste lane capacity (tokens that would EXPIRE UNUSED at reset, from route_economics) is reclaimed for it
     first. Default = deferrable, so bulk batches unless the caller flags urgency.

  2. The SHARED lane budget, allocated across the WHOLE set. The lane's [free .. remaining−reserve] capacity is ONE
     scarce pool every group draws from. This allocates it in PRIORITY order — URGENT groups first, then deferrable
     waste-reclaim — so two groups never both bank the same ~free tokens, and the scarce capacity funds the work that
     most needs it. Urgent-first is also COST-optimal: a ~free lane token spent on an urgent group averts the costlier
     metered-REALTIME overflow, worth more than averting a deferrable group's cheap BATCH overflow. The interactive
     RESERVE is held out, exactly as in route_economics.

Pure COST+CAPACITY+TIME ARITHMETIC on MEASURED inputs (lane_economics eff/remaining/reset + pricing) — never a meaning
decision. NO FORECAST of unknown future work: the plan is the SUBMITTED set against the CURRENT horizon state (this
window's capacity + the reset grid). It degrades HONESTLY — an unpriced batch/realtime model or an unconverged lane is
said out loud and the affected tokens are reported as `unpriced`, never invented at $0.
"""
import datetime

from . import pricing, route_economics

URGENT, DEFERRABLE = "urgent", "deferrable"


def normalize_urgency(value, default=DEFERRABLE):
    """Coerce a caller-DECLARED urgency to the CLOSED two-value enum URGENT|DEFERRABLE — a strict EQUALITY parse of two
    declared tokens, NEVER a free-text meaning guess. None or '' → `default` (the caller declared nothing, so the
    documented default applies). Exactly 'urgent' → URGENT; exactly 'deferrable' → DEFERRABLE (case/space-insensitive).
    ANY OTHER value RAISES ValueError: spendguard does NOT decide what an arbitrary word like 'critical' or 'ASAP'
    means — that is a judgement, and urgency is a FACT the caller states, not text for code to interpret. whole_job
    validates each job's urgency up front and REFUSES one outside the enum, so a mis-declared urgency is surfaced
    loudly, never silently routed to the wrong path."""
    if value is None:
        return default
    v = str(value).strip().lower()
    if v == "":
        return default
    if v == URGENT:
        return URGENT
    if v == DEFERRABLE:
        return DEFERRABLE
    raise ValueError("urgency must be %r or %r (a caller-declared fact) — got %r; spendguard does not guess what an "
                     "arbitrary urgency word means" % (URGENT, DEFERRABLE, value))


def _realtime_per_tok(model, in_tok, out_tok):
    """Metered REALTIME $/token for an urgent group's overflow (pricing.realtime_cost ÷ the call's tokens). None when
    the model has no canonical price — the caller then cannot price urgent overflow and says so, never invents it."""
    if not model:
        return None
    try:
        per = pricing.realtime_cost(model, int(in_tok), int(out_tok))
    except Exception:
        return None
    tot = max(1, int(in_tok) + int(out_tok))
    return (float(per) / tot) if per is not None else None


def plan_horizon(groups, *, lane_binding=None, lane_label=None, batch_model=None, realtime_model=None,
                 reserve_frac=0.2, converged=True, now=None):
    """Allocate `groups` across the lane / batch / realtime over the horizon. Each group is
    {intent, n, in_tok, out_tok, urgency}. Returns {allocations:[…], totals, lane, reset_in_days, why}. PURE arithmetic;
    $0. The lane's shared [free .. remaining−reserve] pool is drawn down across groups URGENT-FIRST (then deferrable
    waste-reclaim); urgent overflow → metered REALTIME, deferrable overflow → BATCH. `lane_binding` is a
    lane_economics.economics() binding bucket; degrade honestly when it is absent/unconverged (lane pool = 0)."""
    now = now or datetime.datetime.now(datetime.timezone.utc)
    lane_ok = bool(lane_binding) and converged and lane_binding.get("eff_usd_per_tok") is not None
    if lane_ok:
        eff, free_total, reserve_tokens, lane_budget = route_economics._lane_geometry(lane_binding, reserve_frac, now)
    else:
        eff = free_total = reserve_tokens = lane_budget = 0.0
    # TWO DISJOINT shared pools: ~free (would-expire) tokens, and the eff-priced lane capacity beyond them.
    free_left = float(free_total)
    eff_left = max(0.0, float(lane_budget) - float(free_total))
    reset_days = route_economics._days_until((lane_binding or {}).get("reset_ts"), now) if lane_binding else None

    # URGENT groups first for the scarce lane capacity (averting the costlier realtime overflow beats averting batch),
    # then deferrable. A stable sub-order (original index) keeps the plan deterministic + reproducible.
    ordered = sorted(enumerate(groups or []), key=lambda iv: (0 if normalize_urgency(iv[1].get("urgency")) == URGENT else 1, iv[0]))

    allocations, totals = [], {"lane_free_tok": 0.0, "lane_eff_tok": 0.0, "batch_tok": 0.0, "meter_tok": 0.0,
                               "unpriced_tok": 0.0, "usd": 0.0}
    for _idx, g in ordered:
        urg = normalize_urgency(g.get("urgency"))
        n = max(0, int(g.get("n") or 0))
        in_tok, out_tok = max(0, int(g.get("in_tok") or 0)), max(0, int(g.get("out_tok") or 0))
        V = float(n * (in_tok + out_tok))
        batch_per_tok = None
        bj = route_economics._batch_cost(batch_model, n, in_tok, out_tok) if batch_model else None
        if bj is not None and V > 0:
            batch_per_tok = bj / V
        rt_per_tok = _realtime_per_tok(realtime_model, in_tok, out_tok) if urg == URGENT else None

        take_free = min(V, free_left)            # reclaim use-it-or-lose-it capacity for BOTH kinds (it is ~$0 either way)
        free_left -= take_free
        rem = V - take_free
        take_eff = batch_tok = meter_tok = unpriced_tok = 0.0
        if urg == URGENT:
            # must run now: lane eff next, then METERED REALTIME overflow (fast). Never batch.
            take_eff = min(rem, eff_left); eff_left -= take_eff; rem -= take_eff
            if rem > 0 and rt_per_tok is not None:
                meter_tok = rem; rem = 0.0
            else:
                unpriced_tok = rem; rem = 0.0           # no priced realtime model → overflow is UNPRICED, said out loud
            over_rate, over_tok = rt_per_tok, meter_tok
            method = "realtime" if (meter_tok or not take_eff) else ("lane+realtime" if meter_tok else "lane")
        else:
            # can wait: after the free reclaim, take lane-eff ONLY if it undercuts batch; the rest to cheap cap-free BATCH.
            use_eff = (batch_per_tok is None) or (eff < batch_per_tok)
            if use_eff:
                take_eff = min(rem, eff_left); eff_left -= take_eff; rem -= take_eff
            if rem > 0 and batch_per_tok is not None:
                batch_tok = rem; rem = 0.0
            elif rem > 0 and eff_left <= 0 and batch_per_tok is None:
                # nothing left to price it with (no batch model, lane exhausted) — honest unpriced, never $0.
                unpriced_tok = rem; rem = 0.0
            else:
                unpriced_tok += rem; rem = 0.0
            over_rate, over_tok = batch_per_tok, batch_tok
            method = "batch" if (batch_tok and not take_free and not take_eff) else (
                     "combo" if (batch_tok or take_eff) else ("lane" if take_free else "unpriced"))
        usd = take_eff * eff + (over_tok * (over_rate or 0.0))
        allocations.append({
            "intent": g.get("intent"), "urgency": urg, "n": n, "tokens": int(V), "method": method,
            "lane_free_tok": round(take_free, 1), "lane_eff_tok": round(take_eff, 1),
            "batch_tok": round(batch_tok, 1), "meter_tok": round(meter_tok, 1), "unpriced_tok": round(unpriced_tok, 1),
            "usd": (round(usd, 6) if unpriced_tok <= 0 else None),
            "unpriced": unpriced_tok > 0})
        for k, v in (("lane_free_tok", take_free), ("lane_eff_tok", take_eff), ("batch_tok", batch_tok),
                     ("meter_tok", meter_tok), ("unpriced_tok", unpriced_tok)):
            totals[k] += v
        totals["usd"] += usd
    totals = {k: (round(v, 6) if k == "usd" else round(v, 1)) for k, v in totals.items()}
    any_unpriced = any(a["unpriced"] for a in allocations)
    totals["usd"] = (totals["usd"] if not any_unpriced else None)
    why = _plan_why(allocations, lane_ok, lane_label, reset_days, totals)
    return {"allocations": allocations, "totals": totals, "lane": lane_label, "lane_converged": lane_ok,
            "reset_in_days": (round(reset_days, 2) if reset_days is not None else None), "why": why}


def _plan_why(allocations, lane_ok, lane_label, reset_days, totals):
    """A human one-liner for the whole plan — what rode the lane free/eff, what went to batch vs realtime, and the
    horizon note (deferrable work could ride the NEXT window free if it can wait that long)."""
    if not allocations:
        return "no groups to plan."
    u = sum(1 for a in allocations if a["urgency"] == URGENT)
    bits = []
    if not lane_ok:
        bits.append("lane not converged (no ~free capacity priced) — urgent→realtime, deferrable→batch")
    else:
        bits.append("%s ~free lane tok reclaimed (would expire at reset)" % format(int(totals["lane_free_tok"]), ","))
    if totals["meter_tok"]:
        bits.append("%s urgent overflow tok → metered realtime" % format(int(totals["meter_tok"]), ","))
    if totals["batch_tok"]:
        bits.append("%s deferrable tok → batch (cap-free)" % format(int(totals["batch_tok"]), ","))
    if totals["unpriced_tok"]:
        bits.append("%s tok UNPRICED (no priced path) — said out loud, never $0" % format(int(totals["unpriced_tok"]), ","))
    tail = ""
    if reset_days is not None and totals["batch_tok"] and lane_ok:
        tail = (" · window resets in %.1fd — deferrable work that can wait could ride the next window's fresh lane "
                "capacity instead of batching (no forecast is made here; batched for certainty)." % reset_days)
    return ("%d urgent / %d deferrable group(s): " % (u, len(allocations) - u)) + "; ".join(bits) + tail


def horizon_report(groups, *, lane=None, batch_model=None, realtime_model=None, reserve_frac=None, now=None):
    """Resolve the LIVE horizon inputs and run plan_horizon, returning its result plus a `resolved` block naming every
    input + its basis (auditable, 'estimating' visible) — the horizon twin of route_economics.route_report. `groups`
    is [{intent, n, in_tok?, out_tok?, urgency?}]; a missing out_tok is estimated per-intent via expected_output, a
    missing in_tok defaults to out_tok. $0 — only measured state is read, no call is made."""
    from . import lane_economics, config, expected_output
    resolved = {"basis": {}}
    bm = batch_model or config._cfg_get("advisor", "batch_model", None)
    rm = realtime_model or config.advisor_model()
    resolved["batch_model"] = bm
    resolved["realtime_model"] = rm
    resolved["basis"]["batch_model"] = "arg" if batch_model else ("config advisor.batch_model" if bm else "UNSET")
    resolved["basis"]["realtime_model"] = "arg" if realtime_model else "config advisor.model"

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

    rf = float(reserve_frac if reserve_frac is not None else config._cfg_get("advisor", "route_reserve_frac", 0.2) or 0.2)
    resolved["reserve_frac"] = rf

    norm = []
    for g in (groups or []):
        intent = g.get("intent")
        out_tok = g.get("out_tok")
        if out_tok is None:
            est, basis = expected_output.expect(bm or (row or {}).get("lane") or "", sig=intent)
            out_tok = int(est or 0)
        in_tok = g.get("in_tok")
        in_tok = int(in_tok) if in_tok is not None else int(out_tok)
        norm.append({"intent": intent, "n": int(g.get("n") or 0), "in_tok": int(in_tok), "out_tok": int(out_tok),
                     "urgency": normalize_urgency(g.get("urgency"))})
    res = plan_horizon(norm, lane_binding=binding, lane_label=(row or {}).get("lane"), batch_model=bm,
                       realtime_model=rm, reserve_frac=rf, converged=converged, now=now)
    res["resolved"] = resolved
    return res


def cmd(argv):
    """CLI: spendguard route-horizon <intent:n[:urgency]> ... [--in TOK] [--out TOK] [--lane L] [--batch-model M] [--json]
    — plan a SET of intent-groups across lane/batch/realtime over the horizon, honouring urgency + the shared lane
    budget. Each positional is `intent:n` or `intent:n:urgent` (default deferrable). $0, read-only."""
    import argparse
    import json as _json
    ap = argparse.ArgumentParser(prog="spendguard route-horizon")
    ap.add_argument("groups", nargs="+", help="intent:n[:urgent|deferrable] — e.g. loinc-map:5000 triage:50:urgent")
    ap.add_argument("--in", dest="in_tok", type=int, default=None, help="input tokens per call (default: = --out)")
    ap.add_argument("--out", dest="out_tok", type=int, default=None, help="output tokens per call (default: measured p90 per intent)")
    ap.add_argument("--lane", default=None, help="force a lane (default: the first converged one)")
    ap.add_argument("--batch-model", dest="batch_model", default=None, help="metered model for the batch leg (default: config advisor.batch_model)")
    ap.add_argument("--json", action="store_true", help="emit the raw result as JSON")
    a = ap.parse_args(argv)
    parsed = []
    for spec in a.groups:
        parts = spec.split(":")
        if len(parts) < 2 or not parts[1].isdigit():
            ap.error("each group is intent:n[:urgent|deferrable] — got %r" % spec)
        parsed.append({"intent": parts[0], "n": int(parts[1]),
                       "urgency": (parts[2] if len(parts) > 2 else DEFERRABLE),
                       "in_tok": a.in_tok, "out_tok": a.out_tok})
    r = horizon_report(parsed, lane=a.lane, batch_model=a.batch_model)
    if a.json:
        print(_json.dumps(r, indent=2, default=str))
        return 0
    rv = r["resolved"]
    _ln = ((", eff $%.2f/Mtok, %s tok left" % (rv.get("lane_eff_usd_per_mtok") or 0,
           format(int(rv.get("lane_remaining_abs") or 0), ","))) if rv.get("lane_converged") else ", NOT converged")
    print("\nroute-horizon — %d group(s) · lane=%s (%s%s) · batch=%s · realtime=%s · reserve=%.0f%%"
          % (len(r["allocations"]), rv.get("lane"), rv["basis"].get("lane"), _ln, rv.get("batch_model"),
             rv.get("realtime_model"), (rv.get("reserve_frac") or 0) * 100))
    if r.get("reset_in_days") is not None:
        print("  window resets in %.1f day(s)" % r["reset_in_days"])
    print("\n  %-22s %-10s %-9s %s" % ("INTENT (urgency)", "METHOD", "COST", "lane-free / lane-eff / batch / meter / unpriced tok"))
    for al in r["allocations"]:
        print("  %-22s %-10s %-9s %s / %s / %s / %s / %s" % (
            ("%s (%s)" % (al["intent"], al["urgency"]))[:22], al["method"], route_economics._fmt_usd(al["usd"]),
            format(int(al["lane_free_tok"]), ","), format(int(al["lane_eff_tok"]), ","),
            format(int(al["batch_tok"]), ","), format(int(al["meter_tok"]), ","), format(int(al["unpriced_tok"]), ",")))
    t = r["totals"]
    print("\n  TOTAL: %s  (free %s + eff %s + batch %s + meter %s + unpriced %s tok)" % (
        route_economics._fmt_usd(t["usd"]), format(int(t["lane_free_tok"]), ","), format(int(t["lane_eff_tok"]), ","),
        format(int(t["batch_tok"]), ","), format(int(t["meter_tok"]), ","), format(int(t["unpriced_tok"]), ",")))
    print("    %s\n" % r["why"])
    return 0
