"""Routing UTILITY — one comparable score per routing TARGET (a subscription LANE or a metered provider:model), so
the router spends wisely: drain the FREE plans first by REAL headroom, and fall to the CHEAPEST metered that still
has prepay only when the lanes can't serve. The two score families are comparable by construction:

    lane utility    = 1.0 + remaining_frac * urgency        (>= 1.0 — free, so above every metered target)
    metered utility = 1.0 - normalized_cost                 (in (0,1) — paid; cheaper per token → higher)

so ANY live lane with headroom outranks EVERY metered target (a $0 plan call beats paying). Among lanes: more
headroom, and a SOONER reset, score higher — unused weekly quota is lost at reset, so a soon-resetting lane with room
is "use it or lose it" (the urgency multiplier). Among metered: cheaper-per-token wins, and a provider whose
available prepay can't cover the estimated call is UNAVAILABLE — except an on_demand/payg account, which reloads and
is never gated on balance. A can't-check balance ('unknown') does NOT block (a gap is not an exhaustion).

Operator policy (chosen): NO hard headroom floor — a low-headroom lane simply sorts low; the reactive cooldown
(resource_state.cooling) stays the only HARD cutoff, at 0%/error. This module SCORES and RANKS; wiring the ranking
into dispatch is route_decision/idle_lanes (the next stage). All tunables are config (advisor.*), never magic here.
"""
import time

from . import config


def _fmt_tok_short(n):
    """Compact token count for a rationale string: 1.4M / 525K / 900. (Local so route_utility stays import-cycle-free.)"""
    n = float(n)
    if n >= 1e6:
        return f"{n / 1e6:.1f}M"
    if n >= 1e3:
        return f"{n / 1e3:.0f}K"
    return f"{n:.0f}"


def _cfg_num(key, default):
    try:
        return float(config._cfg_get("advisor", key, default))
    except Exception:
        return float(default)


def _urgency(reset_ts, now=None):
    """Use-it-or-lose-it multiplier in [1, lane_urgency_max]: 1.0 when the reset is far (>= lane_urgency_horizon_s),
    rising to the max as it nears (quota that resets soon is worth spending NOW, before it is lost). An unknown reset
    → 1.0 (no boost — never invent urgency)."""
    if not reset_ts:
        return 1.0
    now = now if now is not None else time.time()
    ttr = float(reset_ts) - now
    horizon = _cfg_num("lane_urgency_horizon_s", 86400.0)
    umax = _cfg_num("lane_urgency_max", 3.0)
    if ttr <= 0:
        return umax
    frac_near = max(0.0, 1.0 - min(ttr, horizon) / horizon)     # 0 when far, → 1 as the reset approaches
    return 1.0 + frac_near * (umax - 1.0)


def lane_score(row, now=None, abs_norm=None, pace=None):
    """Utility of routing one unit to this subscription lane, or None when its headroom is UNKNOWN (the caller then
    falls back to the call-volume proxy — a can't-know is not a zero). `row` is a lanes.lane_headroom() entry.
    Always >= 1.0 (free), so it outranks any metered target; more headroom × urgency → higher.

    WATER-FILLING on ABSOLUTE tokens when the cap is measured: `remaining_abs` (binding bucket, from lane_economics)
    normalized by `abs_norm` (the max absolute-remaining across the lanes being ranked) is the headroom fraction —
    so a big-cap plan at 30% outranks a small-cap plan at 30% (it has more real tokens to give). Falls back to the
    plan's own remaining_pct FRACTION when the cap is not yet measured (abs unknown) or no abs_norm was supplied, so
    behaviour is unchanged until a cap exists. Either way the score stays >= 1.0 (a plan token is free).

    PACE (lane_economics.pace_by_lane, optional): a plan BEHIND pace (positive — spent less than the elapsed window
    fraction) gets a bonus so discretionary work fills it before its allowance is wasted at reset; a plan AHEAD of
    pace gets no bonus (baseline), so among free lanes the under-used one wins. This is what makes 'use every plan to
    ~100% of its window, never over' automatic. (Shedding an ahead-of-pace PROTECTED plan is done in rank_lanes.)"""
    if not row.get("known") or row.get("remaining_pct") is None:
        return None
    ra = row.get("remaining_abs")
    if ra is not None and abs_norm:
        rf = max(0.0, min(1.0, float(ra) / float(abs_norm)))
    else:
        rf = max(0.0, min(1.0, float(row["remaining_pct"]) / 100.0))
    score = 1.0 + rf * _urgency(row.get("reset_ts"), now)
    if pace is not None:
        score += max(0.0, float(pace)) * _cfg_num("lane_pace_weight", 2.0)
    return score


def _protect_policy():
    """(lane)->bool from config subscription.pace: a lane whose policy is 'protect' (or 'conservative') is SHED off
    for discretionary work once it is ahead of pace — its remaining allowance is preserved for its window, for the
    work only IT can do (e.g. Claude Max weekly, when interactive coding can't run anywhere else). Default: no
    protection — 'maximize', every plan filled to ~100% of its window. General: any user marks any plan to protect."""
    from . import lane_economics
    def _p(lane):
        return lane_economics.pace_policy(lane) in ("protect", "conservative")
    return _p


def rank_lanes(rows, cooling=None, now=None, pace_by=None, protect=None):
    """EVERY enabled lane ordered BEST-first by utility — nothing is silently dropped: a cooling lane appears with
    available=False and a reason, so 'why isn't it routing to lane X' is answerable from the output. `rows` from
    lanes.lane_headroom(); `cooling(lane)->bool` defaults to resource_state. Each entry: {lane, provider, available,
    score, remaining_pct, reset_ts, pace, why}. Order: available+scored (best utility, PACE-weighted) → available+
    UNKNOWN headroom → unavailable (cooling / protected-and-ahead) last. Callers route to the first available=True.
    PACE-aware: `pace_by` {lane: pace_headroom} (default: lane_economics.pace_by_lane) — behind-pace plans score
    higher (fill the paid capacity before reset); a PROTECTED plan that is ahead of pace is held out (`protect`)."""
    if cooling is None:
        from . import resource_state
        cooling = lambda ln: resource_state.cooling(resource_state.lane_key(ln))
    if pace_by is None:
        try:
            from . import lane_economics
            pace_by = lane_economics.pace_by_lane(rows, now=now)
        except Exception:
            pace_by = {}
    if protect is None:
        protect = _protect_policy()
    # WATER-FILL normalizer: the largest ABSOLUTE tokens-left across these lanes, so remaining_abs becomes a fraction
    # comparable to remaining_pct. None when no lane has a measured cap yet → every lane falls back to its own %.
    abs_norm = max((float(r["remaining_abs"]) for r in rows if r.get("remaining_abs") is not None), default=None)
    out = []
    for r in rows:
        pc = (pace_by or {}).get(r["lane"])
        base = {"lane": r["lane"], "provider": r.get("provider"), "remaining_pct": r.get("remaining_pct"),
                "reset_ts": r.get("reset_ts"), "remaining_abs": r.get("remaining_abs"), "pace": pc}
        if cooling(r["lane"]):
            out.append({**base, "available": False, "score": None, "why": "cooling — excluded (reactive backstop)"})
            continue
        if pc is not None and pc < 0 and protect(r["lane"]):
            out.append({**base, "available": False, "score": None,
                        "why": f"protected + ahead of pace ({pc:+.2f}) — held for its own window"})
            continue
        s = lane_score(r, now, abs_norm=abs_norm, pace=pc)
        if s is None:
            why = "unknown headroom (proxy orders it)"
        elif r.get("remaining_abs") is not None and abs_norm:
            why = f"{_fmt_tok_short(r['remaining_abs'])} tok left × urgency {_urgency(r.get('reset_ts'), now):.2f}"
        else:
            why = f"{int(r['remaining_pct'])}% left × urgency {_urgency(r.get('reset_ts'), now):.2f}"
        if pc is not None:
            why += f" · pace {pc:+.2f} ({'behind→fill' if pc >= 0 else 'ahead→ease'})"
        out.append({**base, "available": True, "score": s, "why": why})
    # available first; among available, scored (higher better) before unknown-headroom; unavailable (cooling) last
    out.sort(key=lambda d: (not d["available"], d["score"] is None, -(d["score"] if d["score"] is not None else 0.0)))
    return out


def _metered_available(provider, est_cost):
    """Can this metered provider cover a call costing ~est_cost? (available, why). on_demand/payg reloads → always
    yes. sunk_pool → only if the available balance covers it. unknown balance → yes (a can't-check is not a block).
    None est_cost → yes (nothing to gate on)."""
    from . import balances
    try:
        b = balances.vendor_balance(provider)
    except Exception:
        return True, "balance unchecked"
    kind, avail = b.get("kind"), b.get("available")
    if kind == "on_demand" or (b.get("auto_topup")) or (balances._declared(provider).get("payg")):
        return True, "on_demand/payg (reloads)"
    if avail is None:
        return True, "balance unknown"
    if est_cost is not None and float(avail) < float(est_cost):
        return False, f"balance ${float(avail):.2f} < est ${float(est_cost):.4f}"
    return True, f"balance ${float(avail):.2f}"


def rank_metered(candidates, est_in, est_out):
    """EVERY metered provider:model candidate ordered BEST-first by utility = 1.0 - normalized_cost — nothing is
    silently dropped: one whose prepay can't cover the estimated call appears with available=False and the reason
    (so 'why isn't it using provider X' is answerable). `candidates` = ["provider:model", …]. Cost from
    pricing.realtime_cost; normalized across the AVAILABLE candidates so the cheapest scores highest and all sit in
    (0,1] below any live lane. Order: affordable (cheapest first) → unpriced → unavailable last. Callers route to the
    first available=True."""
    from . import pricing
    rows = []
    for spec in candidates:
        prov = spec.split(":", 1)[0]
        try:
            cost = pricing.realtime_cost(spec, est_in, est_out)
        except Exception:
            cost = None
        ok, why = _metered_available(prov, cost)
        rows.append({"target": spec, "provider": prov, "cost": cost, "available": ok, "why": why})
    known = [p["cost"] for p in rows if p["available"] and p["cost"] is not None]
    hi = max(known) if known else 0.0
    for p in rows:
        if not p["available"]:
            p["score"] = None                               # can't pay → surfaced, not dropped; sorts last
            p["why"] = "unavailable — " + p["why"]
        elif p["cost"] is None:
            p["score"] = 0.01                               # unpriced → just above zero, ranks last among affordable
            p["why"] = "unpriced; " + p["why"]
        else:
            norm = (p["cost"] / hi) if hi > 0 else 0.0
            p["score"] = 1.0 - norm                         # cheapest → ~1.0 (still < any live lane), dearest → ~0
            p["why"] = f"${p['cost']:.4f}/call ({p['why']})"
    rows.sort(key=lambda d: (not d["available"], -(d["score"] if d["score"] is not None else -1.0)))
    return rows


def rank_targets(lane_rows, metered_candidates, est_in, est_out, cooling=None, now=None):
    """The unified ranking the router reads: every available LANE (free, scored >=1) then every affordable METERED
    target (paid, scored <1), best-first. Lanes with known headroom lead by utility; unknown-headroom lanes follow
    (still above metered — a free call, just no quota signal); metered last, cheapest-affordable first. Each entry:
    {kind 'lane'|'metered', target, score|None, why}."""
    lanes_ranked = rank_lanes(lane_rows, cooling=cooling, now=now)
    metered_ranked = rank_metered(metered_candidates, est_in, est_out)
    out = [{"kind": "lane", "target": l["lane"], "provider": l.get("provider"), "available": l["available"],
            "score": l["score"], "why": l["why"]} for l in lanes_ranked]
    out += [{"kind": "metered", "target": m["target"], "provider": m["provider"], "available": m["available"],
             "score": m["score"], "why": m["why"]} for m in metered_ranked]
    return out


def breach_policy():
    """The on-cap-breach routing policy — the Cloudflare 'downgrade vs hard-block' choice, spendguard-shaped:
      • 'refuse'    (DEFAULT) — fail-closed, the identity: a breach hard-refuses.
      • 'downgrade' (opt-in)  — on a breach, name the cheapest AVAILABLE $0 subscription lane to route to instead.
    config caps.on_breach / env SPENDGUARD_ON_BREACH. Anything unrecognised → 'refuse' (never silently loosen)."""
    import os
    v = os.getenv("SPENDGUARD_ON_BREACH")
    if v is None:
        v = config._cfg_get("caps", "on_breach", "refuse")
    v = str(v or "refuse").strip().lower()
    return v if v in ("refuse", "downgrade") else "refuse"


def breach_decision(policy=None):
    """(action, target, why) for a cap breach. 'refuse' → ('refuse', None, why): the caller hard-refuses (fail-closed
    default). 'downgrade' → the cheapest AVAILABLE $0 lane to route to instead ('downgrade', lane, why), or refuse
    when no idle lane has headroom. A $0 plan call is always cheaper than the metered call being refused, so the
    top-ranked available lane IS the downgrade. Reads the PERSISTED lane-headroom snapshot — no network on the hot
    gate path — and never raises (a bookkeeping error degrades to 'refuse', the safe direction)."""
    pol = (policy or breach_policy())
    if pol != "downgrade":
        return "refuse", None, "on_breach=refuse (fail-closed)"
    try:
        from . import lanes
        top = next((d for d in rank_lanes(lanes.lane_headroom(do_fetch=False)) if d.get("available")), None)
        if top:
            return "downgrade", top["lane"], f"route to the {top['lane']} plan ($0) instead — {top['why']}"
    except Exception:
        pass
    return "refuse", None, "on_breach=downgrade, but no idle lane has headroom now → refuse"


# ── Routing GROUPS ("tiers"): a fungible caller asks for a group NAME the USER declared (advisor.tiers), not a
#    pinned model, so the value router can water-fill that group across whichever $0 lane / cheapest credit is best
#    RIGHT NOW. A group is the USER's declaration that those models are interchangeable FOR THEIR PURPOSE — the
#    judgement of what any model can do is made by the human who knows the models, at config time, and recorded here
#    as policy; THE CODE ASSERTS NOTHING about a model's capability and ships NO built-in groups. Unset advisor.tiers
#    → no group routing (the caller keeps its normal path). Same kind of user-declared config as advisor.lane_models
#    / subscription.lane_plans — routing among the user's OWN handful of plans, not classifying arbitrary models. ──


def tiers():
    """{group: [models the user declared interchangeable for it]} from config advisor.tiers ({} if unset). A caller
    asks for a GROUP NAME rather than pinning a model; the router then picks the best-value AVAILABLE lane/model the
    USER placed in that group. The code does NOT decide which models belong to a group — that judgement is the
    user's, applied as policy (identical in kind to advisor.lane_models). Whoever knows the models makes the call,
    once, in config; a model that appears/changes is a config edit, exactly like every other model list in the repo."""
    cfg = config._cfg_get("advisor", "tiers", None)
    out = {}
    if isinstance(cfg, dict):
        for k, v in cfg.items():
            if isinstance(v, (list, tuple)):
                out[str(k)] = list(v)
    return out


def tier_models(tier):
    """The models the user declared for a routing group (or [] for an undeclared group)."""
    return list(tiers().get(tier, []))


def rank_for_tier(tier, est_in=0, est_out=0, lane_rows=None, cooling=None, now=None):
    """VALUE-RANKED targets for a fungible call in the user-declared routing group `tier`, best-first: available $0
    subscription lanes whose configured model the USER placed in that group (PACE-aware — behind-pace first; a
    protected, ahead-of-pace plan excluded) → then the cheapest metered group-model that still has prepay → then
    metered. Each: {kind 'lane'|'metered', target, provider, available, score, why}. The caller routes to the FIRST
    available=True. This is the single entry point that makes balance AUTOMATIC: no pinned model, no per-caller lane
    logic — name a group you declared, the economics pick the plan. Empty when the group is undeclared (advisor.tiers)."""
    from . import lanes as _lanes, lane_catalog, adapters
    rows = list(lane_rows if lane_rows is not None else _lanes.lane_headroom(do_fetch=False))
    tset = set(tier_models(tier))
    lane_model = {}
    for ln in lane_catalog.lanes():
        try:
            lane_model[ln] = lane_catalog.configured_base(ln)
        except Exception:
            lane_model[ln] = None
    tier_lane_rows = [r for r in rows if lane_model.get(r["lane"]) in tset]
    ranked_lanes = rank_lanes(tier_lane_rows, cooling=cooling, now=now)
    served = {m for m in lane_model.values() if m in tset}       # models a $0 lane already offers → don't also pay
    metered_cands = []
    for m in tier_models(tier):
        if m in served:
            continue
        try:
            metered_cands.append("%s:%s" % (adapters.provider_for(m), m))
        except Exception:
            pass
    ranked_metered = rank_metered(metered_cands, est_in, est_out)
    out = [{"kind": "lane", "target": l["lane"], "provider": l.get("provider"), "available": l["available"],
            "score": l["score"], "why": l["why"]} for l in ranked_lanes]
    out += [{"kind": "metered", "target": m["target"], "provider": m["provider"], "available": m["available"],
             "score": m["score"], "why": m["why"]} for m in ranked_metered]
    return out
