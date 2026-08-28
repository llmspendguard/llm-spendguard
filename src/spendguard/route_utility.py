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


def lane_score(row, now=None):
    """Utility of routing one unit to this subscription lane, or None when its headroom is UNKNOWN (the caller then
    falls back to the call-volume proxy — a can't-know is not a zero). `row` is a lanes.lane_headroom() entry.
    Always >= 1.0 (free), so it outranks any metered target; more headroom × urgency → higher."""
    if not row.get("known") or row.get("remaining_pct") is None:
        return None
    rf = max(0.0, min(1.0, float(row["remaining_pct"]) / 100.0))
    return 1.0 + rf * _urgency(row.get("reset_ts"), now)


def rank_lanes(rows, cooling=None, now=None):
    """EVERY enabled lane ordered BEST-first by utility — nothing is silently dropped: a cooling lane appears with
    available=False and a reason, so 'why isn't it routing to lane X' is answerable from the output. `rows` from
    lanes.lane_headroom(); `cooling(lane)->bool` defaults to resource_state. Each entry: {lane, provider, available,
    score, remaining_pct, reset_ts, why}. Order: available+scored (best utility) → available+UNKNOWN headroom (usable,
    no quota signal — the proxy orders these) → unavailable (cooling) last. Callers route to the first available=True."""
    if cooling is None:
        from . import resource_state
        cooling = lambda ln: resource_state.cooling(resource_state.lane_key(ln))
    out = []
    for r in rows:
        base = {"lane": r["lane"], "provider": r.get("provider"), "remaining_pct": r.get("remaining_pct"),
                "reset_ts": r.get("reset_ts")}
        if cooling(r["lane"]):
            out.append({**base, "available": False, "score": None, "why": "cooling — excluded (reactive backstop)"})
            continue
        s = lane_score(r, now)
        why = ("unknown headroom (proxy orders it)" if s is None
               else f"{int(r['remaining_pct'])}% left × urgency {_urgency(r.get('reset_ts'), now):.2f}")
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
