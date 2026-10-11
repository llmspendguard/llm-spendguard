"""Admission control for ordinary calls whose subscription lane is exhausted."""
import os

from . import config, lane_catalog, lane_registry, lanes, overage


def _threshold():
    return float(config._cfg_get("plan_admission", "remaining_pct", 0.0) or 0.0)


def _shed_set():
    """Lanes to PROACTIVELY shed from — treated as at-risk even when NOT capped, so substitutable work routes to the
    other free lanes to PRESERVE this plan's quota (e.g. keep the claude-code weekly window for the agent). Provider-
    agnostic + user-controlled via advisor.shed_lanes (comma list of lane names). Empty = shed only when actually
    capped/on overage."""
    raw = str(config._cfg_get("advisor", "shed_lanes", "") or "")
    return {s.strip() for s in raw.split(",") if s.strip()}


def lane_risk(lane):
    """Structured plan-axis risk; unknown is not treated as exhausted. A lane is 'at risk' (shed-from) when its plan is
    capped, on paid overage, OR the operator listed it in advisor.shed_lanes (proactive quota preservation — any
    provider, not just claude). `test_isolated` is RECORDED on the output as provenance: the overage probe is skipped
    under SPENDGUARD_TEST_ISOLATED, and that gating env is reported here so the risk verdict is reproducible rather than
    silently host-dependent."""
    test_isolated = bool(os.getenv("SPENDGUARD_TEST_ISOLATED"))   # explicit + recorded (below) — not a hidden env gate
    row = next((r for r in lanes.lane_headroom(do_fetch=False) if r.get("lane") == lane), None)
    remaining = row.get("remaining_pct") if row else None
    capped = bool(row and row.get("known") and remaining is not None and float(remaining) <= _threshold())
    spec = lane_registry.lane_spec(lane) or {}
    paid_overage = bool(not test_isolated and spec.get("subagent_host")
                        and overage.current_overage_status().get("on_overage_now"))
    proactive_shed = lane in _shed_set()
    return {"lane": lane, "remaining_pct": remaining, "known": bool(row and row.get("known")),
            "capped": capped, "paid_overage": paid_overage, "proactive_shed": proactive_shed,
            "test_isolated": test_isolated, "at_risk": capped or paid_overage or proactive_shed}


def _ready(lane):
    """Is `lane` usable as a substitute right now? Enabled, auth not confirmed-down, and its own plan not at-risk.
    OPTIMISTIC on auth: 'ok' (confirmed) AND 'unknown' (a lane whose CLI has no cheap status probe — gemini/zai/kimi
    read 'unknown', which is NOT a logout) are both try-able; only a confirmed logout / missing CLI / unreadable probe
    is excluded. An 'unknown' lane that is actually down fails over gracefully via the reactive ladder — strictly better
    than never routing to it (the old `auth != "ok"` gate excluded every no-status-probe lane, leaving only codex able
    to absorb a shed)."""
    row = next((r for r in lanes.lanes_status().get("lanes", []) if r.get("lane") == lane), None)
    if not row or not row.get("enabled") or row.get("auth") not in ("ok", "unknown"):
        return False
    return not lane_risk(lane)["at_risk"]


def _best_ready_alt_lane(primary, tier="cheap"):
    """The best READY alternative lane to `primary` — a DIFFERENT provider's lane that can serve, ranked by
    route_utility (headroom / least-loaded → spread the shed across free plans, never pile it on one). Returns
    (lane, model) or None. This is the AVAILABILITY core shared by decide()'s fallback tier and ready_meta_model: an
    UNPROVEN but SUBSTITUTABLE call can still be SERVED on a ready free lane rather than refused, because the caller
    declared the model substitutable (a pinned / measurement call never reaches here). $0 (cached status), never raises."""
    from . import lane_catalog, route_utility
    ready = []
    for lane in lane_catalog.lanes():
        if lane == primary or not _ready(lane):
            continue
        m = lane_catalog.lane_model_for_tier(lane, tier) or lane_catalog.configured_base(lane)
        if m:
            ready.append((lane, m))
    if not ready:
        return None
    snapshot = {r["lane"]: r for r in lanes.lane_headroom(do_fetch=False)}
    ranked = route_utility.rank_lanes([
        snapshot.get(lane, {"lane": lane, "provider": lane_catalog.lane_provider(lane), "known": False,
                            "remaining_pct": None, "reset_ts": None})
        for lane, _m in ready])
    order = {row["lane"]: n for n, row in enumerate(ranked) if row.get("available")}
    pick = sorted((it for it in ready if it[0] in order), key=lambda it: order[it[0]])
    return pick[0] if pick else None


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
    # AVAILABILITY FALLBACK — the proper shed. No PROVEN substitute exists for this intent, but this is a SUBSTITUTABLE
    # call (a caller model-pin / measurement never reaches decide — the `not _caller_model_pin` gate in adapters.call),
    # so SERVE it on the best READY alternative lane instead of refusing. The proven substitute above is PREFERRED
    # (quality-measured); this tier serves substitutable work on any ready free lane (same ranking ready_meta_model
    # uses), which is what lanes are FOR. Only refuse when NOTHING ready can serve. Config-gated
    # (plan_admission.availability_fallback, default on) so the old proven-only posture is one setting away.
    if str(config._cfg_get("plan_admission", "availability_fallback", True)).strip().lower() not in ("0", "false", "no", "off"):
        alt = _best_ready_alt_lane(primary)
        if alt:
            lane, chosen = alt
            why = ("plan paid-overage admission → ready lane (substitutable, no confirmed substitute)" if risk["paid_overage"]
                   else ("proactive shed → ready lane (substitutable)" if risk.get("proactive_shed")
                         else f"plan quota admission ({risk['remaining_pct']}% remaining) → ready lane (substitutable)"))
            return {"action": "redirect", "model": chosen, "lane": lane, "reason": why, "requested_model": model}
    # No ready alternative. A PROACTIVELY-shed lane (in advisor.shed_lanes but NOT actually capped/on overage) is still
    # AVAILABLE — shedding is a PREFERENCE, so with nowhere to shed to we KEEP using it rather than refuse. Only a truly
    # capped / paid-overage lane cannot serve, so it refuses (never fail open onto the exhausted lane).
    if risk.get("proactive_shed") and not (risk.get("capped") or risk.get("paid_overage")):
        return {"action": "keep", "model": model}
    return {"action": "refuse", "model": model, "lane": primary,
            "reason": "plan is capped/on paid overage and no ready lane can serve this substitutable call",
            "risk": risk}


def ready_meta_model(default_model):
    """Pick the model for a spendguard-INTERNAL meta/discretionary call (the batchable judge, the served-model
    resolver, best-value synthesis) that rides a READY provider. Provider-AGNOSTIC by design: for spendguard's own
    cheap meta judgements the model is NOT the measurement — any sufficiently-cheap ready provider will do, and the
    point is to exploit the best of N configured providers rather than bleed on, or be REFUSED by, one capped plan
    (the batchable judge was refused under claude-code paid-overage, which left the lane-eligibility figure dark).

    Returns `default_model` when its lane is not at-risk (nothing to change); otherwise the best ready alternative via
    the shared _best_ready_alt_lane (a $0 lane with headroom preferred); else `default_model` (degrade — never refuse a
    meta judgement). It SKIPS decide()'s proven-substitute PREFERENCE tier and goes straight to the availability core:
    a meta judgement is not a measurement, so it needs no per-intent quality proof. (decide() prefers a proven
    substitute first, then falls back to the same availability core for substitutable USER calls.) $0 (cached lane
    status). Never raises — a meta call must not break on a routing read."""
    try:
        from . import adapters
        prov = adapters.provider_for(default_model)
        primary = adapters._LANES.get(prov, (None,))[0]
        if not primary or not lane_risk(primary)["at_risk"]:
            return default_model                          # the default's own plan is healthy → keep it
        alt = _best_ready_alt_lane(primary, tier="cheap")  # shared availability core (same as decide's fallback)
        return alt[1] if alt else default_model           # no other ready provider → degrade, let the normal path handle it
    except Exception:
        return default_model


def risks():
    return [lane_risk(spec["lane"]) for spec in lane_registry.LANES]
