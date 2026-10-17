"""Measured lane token cost and fail-closed plan-quota admission."""
import os

from . import calls, config, lane_economics, lane_registry, lanes

MIN_SAMPLES_FOR_MEASURED = 5
MAX_PLAN_FRACTION_DEFAULT = 0.05
UNMEASURED_FANOUT_REFUSE_SIZE = 3
AVOID_CLAUDE_LANE_DEFAULT = True


class PlanQuotaRefusal(RuntimeError):
    """A deliberate refusal before subscription-plan quota is consumed."""


def _configured_bool(section, key, env_name, default):
    raw = os.environ.get(env_name)
    if raw is None:
        raw = config._cfg_get(section, key, default)
    if isinstance(raw, bool):
        return raw
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def max_plan_fraction():
    raw = os.environ.get("SPENDGUARD_MAX_PLAN_FRACTION")
    if raw is None:
        raw = config._cfg_get("routing", "max_plan_fraction", MAX_PLAN_FRACTION_DEFAULT)
    try:
        return min(1.0, max(0.0, float(raw)))
    except (TypeError, ValueError):
        return MAX_PLAN_FRACTION_DEFAULT


def avoid_anthropic_lane_enabled():
    return _configured_bool("routing", "avoid_claude_lane", "SPENDGUARD_AVOID_CLAUDE_LANE",
                            AVOID_CLAUDE_LANE_DEFAULT)


def measured_lane_token_cost(intent=None):
    """Ledger-derived average tokens/call, preferring an adequately sampled intent slice."""
    con = calls._calls_db()
    lane_expr = "COALESCE(NULLIF(resolved_lane,''),NULLIF(executor,''))"
    params = (intent,) if intent else ()
    suffix = " AND intent=?" if intent else ""
    query = (f"SELECT {lane_expr}, COUNT(*), AVG(COALESCE(in_tok,0)+COALESCE(out_tok,0)) "
             f"FROM calls WHERE {lane_expr} IS NOT NULL{suffix} GROUP BY {lane_expr}")
    intent_rows = {str(lane): (int(count), float(avg or 0.0))
                   for lane, count, avg in con.execute(query, params).fetchall()}
    global_rows = con.execute(
        f"SELECT {lane_expr}, COUNT(*), AVG(COALESCE(in_tok,0)+COALESCE(out_tok,0)) "
        f"FROM calls WHERE {lane_expr} IS NOT NULL GROUP BY {lane_expr}").fetchall()
    out = {}
    for lane, count, avg in global_rows:
        lane = str(lane)
        chosen_count, chosen_avg = intent_rows.get(lane, (int(count), float(avg or 0.0)))
        if chosen_count < MIN_SAMPLES_FOR_MEASURED:
            chosen_count, chosen_avg = int(count), float(avg or 0.0)
        out[lane] = {"tokens_per_call": chosen_avg, "samples": chosen_count,
                     "basis": "measured" if chosen_count >= MIN_SAMPLES_FOR_MEASURED else "unmeasured"}
    return out


def anthropic_lane_names():
    return {name for name in lane_registry.all_lanes()
            if (lane_registry.lane_spec(name) or {}).get("provider") == "anthropic"}


def apply_anthropic_avoidance(candidate_lanes, *, auth_over_quota=False):
    candidates = list(candidate_lanes)
    if not avoid_anthropic_lane_enabled():
        return candidates
    anthropic = anthropic_lane_names()
    alternatives = [lane for lane in candidates if lane not in anthropic]
    if alternatives:
        return alternatives
    if candidates and not auth_over_quota:
        raise PlanQuotaRefusal("Anthropic is the sole ready+capable lane; refusing plan quota while "
                               "routing.avoid_claude_lane is on (pass auth_over_quota=True to authorize).")
    return candidates


def estimate_lane_quota(calls_per_lane, *, intent=None, headroom_rows=None, token_costs=None):
    """Return auditable per-lane quota projections without inventing an unknown cap."""
    headroom_rows = list(headroom_rows if headroom_rows is not None else lanes.lane_headroom(do_fetch=True))
    by_lane = {row.get("lane"): row for row in headroom_rows}
    economics = {row.get("lane"): row for row in lane_economics.economics(headroom_rows=headroom_rows)}
    token_costs = token_costs if token_costs is not None else measured_lane_token_cost(intent)
    statements = []
    for lane, n_calls in calls_per_lane.items():
        cost = token_costs.get(lane) or {"tokens_per_call": None, "samples": 0, "basis": "unmeasured"}
        per_call = cost.get("tokens_per_call")
        est_tokens = float(n_calls) * float(per_call) if per_call is not None else None
        headroom = by_lane.get(lane) or {}
        binding = (economics.get(lane) or {}).get("binding") or {}
        cap = binding.get("cap")
        if cap is None:
            measured_caps = lane_economics.measured_lane_caps(lane)
            cap = min((row["cap"] for row in measured_caps), default=None)
        remaining_pct = headroom.get("remaining_pct")
        quota_known = bool(headroom.get("known") and cap is not None and remaining_pct is not None)
        pct = (100.0 * est_tokens / cap) if est_tokens is not None and cap else None
        projected = (float(remaining_pct) - pct) if remaining_pct is not None and pct is not None else None
        rate = binding.get("eff_usd_per_tok")
        statements.append({"lane": lane, "calls": int(n_calls), **cost, "est_tokens": est_tokens,
                           "cap_tokens": cap, "pct_of_cap": pct, "live_remaining_pct": remaining_pct,
                           "quota_known": quota_known,
                           "projected_remaining_after": projected,
                           "effective_token_usd": est_tokens * rate if est_tokens is not None and rate is not None else None})
    return statements


def quota_preflight(calls_per_lane, *, intent=None, auth_over_quota=False, headroom_rows=None, token_costs=None,
                    emit=None):
    statements = estimate_lane_quota(calls_per_lane, intent=intent, headroom_rows=headroom_rows,
                                     token_costs=token_costs)
    refusals = []
    limit_pct = max_plan_fraction() * 100.0
    for row in statements:
        pct = row["pct_of_cap"]
        row["statement"] = (f"quota lane={row['lane']} calls={row['calls']} est_tokens={row['est_tokens']} "
                            f"effective_token_usd={row['effective_token_usd']} pct_of_cap={pct} "
                            f"headroom={row['live_remaining_pct']}% basis={row['basis']} samples={row['samples']}")
        if emit:
            emit(row["statement"])
        if not row["quota_known"]:
            continue
        if row["basis"] != "measured" and row["calls"] > UNMEASURED_FANOUT_REFUSE_SIZE:
            refusals.append(f"{row['lane']}: unmeasured quota cost for fan of {row['calls']} calls")
        if pct is None:
            if row["calls"] > UNMEASURED_FANOUT_REFUSE_SIZE:
                refusals.append(f"{row['lane']}: cap/headroom cannot be measured; refusing fan closed")
            continue
        if pct > limit_pct:
            refusals.append(f"{row['lane']}: projected {pct:.2f}% of cap exceeds authorized {limit_pct:.2f}%")
        if row["live_remaining_pct"] is None:
            refusals.append(f"{row['lane']}: live headroom unknown; projected {pct:.2f}% cannot be authorized")
        elif pct > float(row["live_remaining_pct"]):
            shortfall = pct - float(row["live_remaining_pct"])
            refusals.append(f"{row['lane']}: projected {pct:.2f}% exceeds headroom "
                            f"{float(row['live_remaining_pct']):.2f}% (shortfall {shortfall:.2f}%)")
    if refusals and not auth_over_quota:
        raise PlanQuotaRefusal("; ".join(refusals) + "; pass auth_over_quota=True to authorize")
    return {"ok": not refusals or auth_over_quota, "refusals": refusals, "lanes": statements}


def planned_round_robin_lane_calls(call_count, lane_names):
    """Return the deterministic pre-dispatch allocation for a lane fan."""
    names = list(lane_names)
    if not names:
        return {}
    base, extra = divmod(int(call_count), len(names))
    return {lane: base + int(index < extra) for index, lane in enumerate(names)}


def enforce_single_call_plan_fraction(lane, projected_tokens, *, auth_over_quota=False, headroom_rows=None,
                                      token_costs=None):
    row = estimate_lane_quota({lane: 1}, headroom_rows=headroom_rows, token_costs=token_costs)[0]
    if not row["quota_known"]:
        return row
    cap = row.get("cap_tokens")
    if cap is None:
        if not auth_over_quota:
            raise PlanQuotaRefusal(f"{lane}: plan availability cannot be measured; refusing call closed "
                                   "(pass auth_over_quota=True to authorize)")
        return row
    fraction = float(projected_tokens) / float(cap)
    if fraction > max_plan_fraction() and not auth_over_quota:
        raise PlanQuotaRefusal(
            f"{lane}: projected call {projected_tokens} tokens / availability {cap} tokens = {fraction:.2%}, "
            f"above {max_plan_fraction():.2%}; pass auth_over_quota=True to authorize")
    return row
