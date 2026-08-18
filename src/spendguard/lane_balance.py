"""Load-balance across subscription LANES by per-plan UTILISATION — the proactive brain of "use every flat-fee plan
well". This layer only SENSES: which plans are HOT (saturated, shed FROM) vs IDLE (spare capacity, absorb overflow).
The routing decision and the dispatch wiring build on top (separately), and the acceptable-substitute set is
model-proposed + confirmed once (Stage B).

HONESTY (stated, not papered over): utilisation here is spendguard's OWN est-VALUE ÷ the flat plan fee — what the
subscription-covered usage WOULD have cost at API rates against what you pay. It is NOT the provider's true
remaining quota (Anthropic Max weekly/5h limits are not API-exposed), so it is a capacity-UTILISATION signal, not a
quota gauge. The reactive lane error stays the hard exhaustion backstop; this is the pacing layer that fills idle
paid capacity. Ash's conversation-mining idea (limit-signals in Claude Code transcripts) will SHARPEN this later.

Numbers come from the receipt's OWN per-source est-value cache, re-windowed the same way — so they MATCH the receipt
rather than being a parallel computation that could disagree.
"""
from . import config, adapters

# Thresholds are CONFIG, never hardcoded: a plan whose est-value is below IDLE_RATIO of its fee has spare capacity;
# above HOT_RATIO of its fee it is saturated. Defaults are starting points, tunable per `advisor.lane_*_ratio`.
IDLE_RATIO_DEFAULT = 0.5
HOT_RATIO_DEFAULT = 1.5


def _util_ratio_cfg(name, default):
    try:
        return float(config._cfg_get("advisor", name, None) or default)
    except (TypeError, ValueError):
        return default


def _lane_fee(lane, n_lanes, total_fee):
    """(fee, exact) per-lane MONTHLY fee. An explicit `subscription.lane_plans` {lane: usd} map wins (exact); else the
    total plan fee split evenly across the lanes (approximate — flagged, never presented as exact). No dollar literal
    lives here — the number is always config-derived."""
    lp = config._cfg_get("subscription", "lane_plans", None) or {}
    if isinstance(lp, dict) and lp.get(lane) is not None:
        try:
            return float(lp[lane]), True
        except (TypeError, ValueError):
            pass
    return (float(total_fee) / max(1, int(n_lanes))), False


def lane_utilization():
    """Per-lane est-value THIS MONTH and its utilisation vs the plan fee, so the router — and the user — can see which
    subscription plans are HOT and which are IDLE.

    Returns {"lanes": [{lane, provider, est_value_month, plan_fee, utilization, fee_exact, state, fresh}], "total_fee",
    "fee_is_default", "asof"}, state ∈ {idle, warm, hot}. Reuses receipt._cache_path / _rewindow / _plan_usd so the
    figures equal the receipt's (and inherit its stale-cache guard: `fresh=False` means the number could be a frozen
    earlier-month value and must be refreshed, never shown as current)."""
    from . import receipt
    import json
    try:
        data = json.loads(receipt._cache_path().read_text()).get("est_value_by_source") or {}
    except Exception:
        data = {}                                         # no cache yet → every lane reads as idle (nothing recorded)
    total_fee, fee_default = receipt._plan_usd()
    idle_r, hot_r = _util_ratio_cfg("lane_idle_ratio", IDLE_RATIO_DEFAULT), _util_ratio_cfg("lane_hot_ratio", HOT_RATIO_DEFAULT)
    # LANE -> provider from the ONE source of truth (adapters._LANES); the est-value SOURCE string is the lane name.
    lane_prov = {lane: prov for prov, (lane, _mod) in adapters._LANES.items()}
    lanes = sorted(lane_prov)
    out = []
    for lane in lanes:
        rec = data.get(lane) or {}
        wins, fresh = receipt._rewindow(rec) if rec else ({"month": 0.0}, True)
        ev = float(wins.get("month") or 0.0)
        fee, fee_exact = _lane_fee(lane, len(lanes), total_fee)
        util = (ev / fee) if fee else None
        state = ("hot" if util is not None and util >= hot_r else
                 "idle" if util is not None and util < idle_r else "warm")
        out.append({"lane": lane, "provider": lane_prov[lane], "est_value_month": round(ev, 2),
                    "plan_fee": round(fee, 2), "utilization": (round(util, 3) if util is not None else None),
                    "fee_exact": fee_exact, "state": state, "fresh": fresh})
    return {"lanes": out, "total_fee": round(float(total_fee), 2), "fee_is_default": fee_default,
            "asof": receipt._windows()[0]}


def hot_lanes():
    """Lanes that are saturated (shed work FROM these)."""
    return [l["lane"] for l in lane_utilization()["lanes"] if l["state"] == "hot"]


def idle_lanes():
    """Lanes with spare capacity (route overflow TO these), least-utilised first — the order the router prefers."""
    idle = [l for l in lane_utilization()["lanes"] if l["state"] == "idle"]
    return [l["lane"] for l in sorted(idle, key=lambda l: (l["utilization"] if l["utilization"] is not None else 0.0))]


def format_utilization():
    """One line per lane for `spendguard lanes --balance` and the router's rationale. Pure est-VALUE (plan usage) —
    split from billed $ per the cost-display rule, and explicitly NOT the provider's quota."""
    u = lane_utilization()
    approx = "" if all(l["fee_exact"] for l in u["lanes"]) else \
        "   (per-lane fee = plan total ÷ lanes; set subscription.lane_plans for exact)"
    star = "*" if u["fee_is_default"] else ""
    head = f"per-plan UTILISATION this month — est-value ÷ plan fee{star}; NOT billed, NOT provider quota:{approx}"
    lines = [head]
    label = {"hot": "🔥 HOT  — shed FROM", "idle": "💤 IDLE — absorb overflow", "warm": "·  ok"}
    for l in u["lanes"]:
        util = f"{l['utilization']:.2f}x" if l["utilization"] is not None else "n/a"
        stale = "" if l["fresh"] else "  (STALE cache — run `spendguard receipt` to refresh)"
        lines.append(f"  {l['lane']:12s} ({l['provider']:9s})  est-value ${l['est_value_month']:>9.2f} / "
                     f"${l['plan_fee']:>6.0f} = {util:>7}  {label[l['state']]}{stale}")
    return "\n".join(lines)
