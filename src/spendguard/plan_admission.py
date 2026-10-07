"""Admission control for ordinary calls whose subscription lane is exhausted."""
import os

from . import config, lane_catalog, lane_registry, lanes, overage


def _threshold():
    return float(config._cfg_get("plan_admission", "remaining_pct", 0.0) or 0.0)


def lane_risk(lane):
    """Structured plan-axis risk; unknown is not treated as exhausted."""
    row = next((r for r in lanes.lane_headroom(do_fetch=False) if r.get("lane") == lane), None)
    remaining = row.get("remaining_pct") if row else None
    capped = bool(row and row.get("known") and remaining is not None and float(remaining) <= _threshold())
    spec = lane_registry.lane_spec(lane) or {}
    paid_overage = bool(not os.getenv("SPENDGUARD_TEST_ISOLATED") and spec.get("subagent_host")
                        and overage.current_overage_status().get("on_overage_now"))
    return {"lane": lane, "remaining_pct": remaining, "known": bool(row and row.get("known")),
            "capped": capped, "paid_overage": paid_overage, "at_risk": capped or paid_overage}


def _ready(lane):
    row = next((r for r in lanes.lanes_status().get("lanes", []) if r.get("lane") == lane), None)
    if not row or not row.get("enabled") or row.get("auth") != "ok":
        return False
    return not lane_risk(lane)["at_risk"]


def decide(model, intent):
    """Return a zero-spend routing decision for a labelled, substitutable call.

    Alternatives come only from the existing propose→confirm registry: the agentic meaning decision has already
    happened. This hot path performs fixed structured filtering and never invents interchangeability.
    """
    from . import adapters, lane_balance, route_utility
    provider = adapters.provider_for(model)
    primary = adapters._LANES.get(provider, (None,))[0]
    if not primary:
        return {"action": "keep", "model": model}
    risk = lane_risk(primary)
    if not risk["at_risk"]:
        return {"action": "keep", "model": model}
    candidates = []
    for candidate in lane_balance.substitutes_for(intent):
        cprov = adapters.provider_for(candidate)
        lane = adapters._LANES.get(cprov, (None,))[0]
        if lane and lane != primary and _ready(lane):
            candidates.append((lane, candidate))
    if candidates:
        snapshot = {r["lane"]: r for r in lanes.lane_headroom(do_fetch=False)}
        ranked = route_utility.rank_lanes([
            snapshot.get(lane, {"lane": lane, "provider": lane_catalog.lane_provider(lane), "known": False,
                                "remaining_pct": None, "reset_ts": None})
            for lane, _model in candidates])
        order = {row["lane"]: n for n, row in enumerate(ranked) if row.get("available")}
        eligible = sorted((item for item in candidates if item[0] in order), key=lambda item: order[item[0]])
        if eligible:
            lane, chosen = eligible[0]
            why = ("plan paid-overage admission" if risk["paid_overage"] else
                   f"plan quota admission ({risk['remaining_pct']}% remaining)")
            return {"action": "redirect", "model": chosen, "lane": lane, "reason": why,
                    "requested_model": model}
    return {"action": "refuse", "model": model, "lane": primary,
            "reason": "plan is capped/on paid overage and no confirmed READY substitute lane exists",
            "risk": risk}


def ready_meta_model(default_model):
    """Pick the model for a spendguard-INTERNAL meta/discretionary call (the batchable judge, the served-model
    resolver, best-value synthesis) that rides a READY provider. Provider-AGNOSTIC by design: for spendguard's own
    cheap meta judgements the model is NOT the measurement — any sufficiently-cheap ready provider will do, and the
    point is to exploit the best of N configured providers rather than bleed on, or be REFUSED by, one capped plan
    (the batchable judge was refused under claude-code paid-overage, which left the lane-eligibility figure dark).

    Returns `default_model` when its lane is not at-risk (nothing to change); otherwise the cheapest configured model
    on another READY lane, ranked by the same route_utility.rank_lanes decide() uses (so a $0 lane with headroom is
    preferred); else `default_model` (degrade — never refuse a meta judgement). UNLIKE decide(), this does NOT require
    a pre-confirmed per-intent substitute: that conservative contract exists to protect USER calls' measurement, which
    a meta judgement is not. $0 (cached lane status). Never raises — a meta call must not break on a routing read."""
    try:
        from . import adapters, lane_catalog, route_utility
        prov = adapters.provider_for(default_model)
        primary = adapters._LANES.get(prov, (None,))[0]
        if not primary or not lane_risk(primary)["at_risk"]:
            return default_model                          # the default's own plan is healthy → keep it
        ready = []
        for lane in lane_catalog.lanes():
            if lane == primary or not _ready(lane):
                continue
            m = lane_catalog.lane_model_for_tier(lane, "cheap") or lane_catalog.configured_base(lane)
            if m:
                ready.append((lane, m))
        if not ready:
            return default_model                          # no other ready provider → degrade, let the normal path handle it
        snapshot = {r["lane"]: r for r in lanes.lane_headroom(do_fetch=False)}
        ranked = route_utility.rank_lanes([
            snapshot.get(lane, {"lane": lane, "provider": lane_catalog.lane_provider(lane), "known": False,
                                "remaining_pct": None, "reset_ts": None})
            for lane, _m in ready])
        order = {row["lane"]: n for n, row in enumerate(ranked) if row.get("available")}
        eligible = sorted((item for item in ready if item[0] in order), key=lambda item: order[item[0]])
        return eligible[0][1] if eligible else default_model
    except Exception:
        return default_model


def risks():
    return [lane_risk(spec["lane"]) for spec in lane_registry.LANES]
