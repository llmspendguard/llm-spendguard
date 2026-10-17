"""Read-only realized-routing history from the calls ledger. No calls, probes, or prices."""

import argparse
import json

from . import calls


_METERED = "cost > 0 AND COALESCE(NULLIF(executor,''),'') IN ('','api')"
_EXECUTOR = "COALESCE(NULLIF(executor,''),'unknown')"
_LANE = "COALESCE(NULLIF(resolved_lane,''),'unknown')"
_SERVED = "COALESCE(NULLIF(served_model,''),'unknown')"


def _route_realized_where_clause(intent, since=None, as_of=None):
    predicates = ["intent=?"]
    args = [intent]
    if since is not None:
        predicates.append("ts>=?")
        args.append(since)
    if as_of is not None:
        predicates.append("ts<=?")
        args.append(as_of)
    return " AND ".join(predicates), args


def _fetch_route_realized_rows(sql, args):
    return calls._route_realized_fetchall(sql, args)


def route_realized_report(intent, *, since=None, as_of=None, limit=20):
    """Aggregate exact stored routing outcomes for one intent and optional ``ts`` window."""
    where, args = _route_realized_where_clause(intent, since, as_of)
    limit = max(0, int(limit))
    totals_row = _fetch_route_realized_rows(
        f"""SELECT COUNT(*),
                   COALESCE(SUM(CASE WHEN {_METERED} THEN cost ELSE 0 END),0),
                   COALESCE(SUM(CASE WHEN {_METERED} THEN 1 ELSE 0 END),0),
                   COALESCE(SUM(CASE WHEN COALESCE(cost,0)<=0 THEN 1 ELSE 0 END),0),
                   COALESCE(SUM(CASE WHEN {_EXECUTOR}='unknown' OR {_LANE}='unknown' THEN 1 ELSE 0 END),0),
                   COALESCE(SUM(CASE WHEN cost>0 THEN 1 ELSE 0 END),0),
                   COALESCE(SUM(CASE WHEN cost>0 THEN cost ELSE 0 END),0),
                   COALESCE(SUM(CASE WHEN COALESCE(cost,0)<=0
                                     AND {_EXECUTOR} NOT IN ('unknown','api') THEN 1 ELSE 0 END),0)
            FROM calls WHERE {where}""", args)[0]
    (jobs, billed_cost, billed_jobs, zero_jobs, unknown_jobs, positive_cost_jobs,
     positive_cost, zero_lane_jobs) = totals_row
    totals = {"jobs": int(jobs), "billed_cost": float(billed_cost or 0),
              "billed_jobs": int(billed_jobs), "zero_cost_jobs": int(zero_jobs),
              "unknown_routing_jobs": int(unknown_jobs)}
    if int(positive_cost_jobs) != int(billed_jobs):
        totals["positive_cost_jobs"] = int(positive_cost_jobs)
        totals["positive_cost"] = float(positive_cost)

    grouped = _fetch_route_realized_rows(
        f"""SELECT {_EXECUTOR}, {_LANE}, COUNT(*),
                   COALESCE(SUM(CASE WHEN {_METERED} THEN cost ELSE 0 END),0),
                   COALESCE(SUM(CASE WHEN {_METERED} THEN 1 ELSE 0 END),0),
                   COALESCE(SUM(CASE WHEN COALESCE(cost,0)<=0 THEN 1 ELSE 0 END),0),
                   GROUP_CONCAT(DISTINCT NULLIF(served_model,'')),
                   GROUP_CONCAT(DISTINCT NULLIF(requested_model,''))
            FROM calls WHERE {where}
            GROUP BY {_EXECUTOR}, {_LANE}
            ORDER BY COUNT(*) DESC, {_EXECUTOR}, {_LANE}""", args)
    by_executor_lane = [
        {"executor": ex, "resolved_lane": lane, "jobs": int(n), "billed_cost": float(cost or 0),
         "billed_jobs": int(billed), "zero_cost_jobs": int(zero),
         "served_models": sorted(models.split(",")) if models else [],
         "requested_models": sorted(requested.split(",")) if requested else []}
        for ex, lane, n, cost, billed, zero, models, requested in grouped
    ]

    model_rows = _fetch_route_realized_rows(
        f"""SELECT {_SERVED}, COUNT(*),
                   COALESCE(SUM(CASE WHEN {_METERED} THEN cost ELSE 0 END),0),
                   COALESCE(SUM(CASE WHEN {_METERED} THEN 1 ELSE 0 END),0),
                   COALESCE(SUM(CASE WHEN COALESCE(cost,0)<=0 THEN 1 ELSE 0 END),0)
            FROM calls WHERE {where} GROUP BY {_SERVED}
            ORDER BY COUNT(*) DESC, {_SERVED}""", args)
    by_served_model = [
        {"served_model": model, "jobs": int(n), "billed_cost": float(cost or 0),
         "billed_jobs": int(billed), "zero_cost_jobs": int(zero)}
        for model, n, cost, billed, zero in model_rows
    ]

    drift_rows = _fetch_route_realized_rows(
        f"""SELECT requested_model, served_model, COUNT(*) FROM calls WHERE {where}
            AND NULLIF(requested_model,'') IS NOT NULL AND NULLIF(served_model,'') IS NOT NULL
            AND requested_model != served_model
            GROUP BY requested_model, served_model ORDER BY COUNT(*) DESC, requested_model, served_model""", args)
    model_drift = [{"requested_model": req, "served_model": served, "jobs": int(n)}
                   for req, served, n in drift_rows]

    fell_where = f"{where} AND (NULLIF(fell_from,'') IS NOT NULL OR NULLIF(fell_reason,'') IS NOT NULL)"
    total_fell = _fetch_route_realized_rows(f"SELECT COUNT(*) FROM calls WHERE {fell_where}", args)[0][0]
    fell_reason_rows = _fetch_route_realized_rows(
        f"""SELECT COALESCE(NULLIF(fell_reason,''),'unknown'), COUNT(*),
                   COALESCE(SUM(CASE WHEN {_METERED} THEN cost ELSE 0 END),0)
            FROM calls WHERE {fell_where}
            GROUP BY COALESCE(NULLIF(fell_reason,''),'unknown') ORDER BY COUNT(*) DESC, 1""", args)
    fell_from_rows = _fetch_route_realized_rows(
        f"""SELECT COALESCE(NULLIF(fell_from,''),'unknown'), COUNT(*) FROM calls WHERE {fell_where}
            GROUP BY COALESCE(NULLIF(fell_from,''),'unknown') ORDER BY COUNT(*) DESC, 1""", args)
    redirect_rows = _fetch_route_realized_rows(
        f"""SELECT COALESCE(NULLIF(redirect_reason,''),'unknown'), COUNT(*) FROM calls WHERE {fell_where}
            GROUP BY COALESCE(NULLIF(redirect_reason,''),'unknown') ORDER BY COUNT(*) DESC, 1""", args)
    fell = {
        "total_fell_jobs": int(total_fell),
        "by_fell_reason": [{"fell_reason": reason, "jobs": int(n), "billed_cost": float(cost or 0)}
                           for reason, n, cost in fell_reason_rows],
        "by_fell_from": [{"fell_from": lane, "jobs": int(n)} for lane, n in fell_from_rows],
        "by_redirect_reason": [{"redirect_reason": reason, "jobs": int(n)} for reason, n in redirect_rows],
    }

    sample_rows = _fetch_route_realized_rows(
        f"""SELECT ts, {_EXECUTOR}, {_LANE}, requested_model, served_model, cost,
                   redirect_reason, fell_reason FROM calls WHERE {where}
            ORDER BY ts DESC LIMIT ?""", args + [limit])
    samples = [dict(zip(("ts", "executor", "resolved_lane", "requested_model", "served_model", "cost",
                         "redirect_reason", "fell_reason"), row)) for row in sample_rows]

    caveats = []
    if unknown_jobs:
        caveats.append(f"{int(unknown_jobs)} rows have NULL/empty executor or resolved_lane (legacy) → bucketed as 'unknown'")
    if not jobs:
        caveats.append("no calls found for this intent in the requested ts window")
    if int(positive_cost_jobs) != int(billed_jobs):
        caveats.append("positive-cost rows outside the metered executor predicate are counted separately as positive_cost_jobs")
    shares = {"metered_share_jobs": (int(billed_jobs) / int(jobs)) if jobs else None,
              "metered_share_cost": (float(billed_cost) / float(positive_cost)) if positive_cost else (0.0 if jobs else None),
              "zero_lane_share_jobs": (int(zero_lane_jobs) / int(jobs)) if jobs else None,
              "unknown_share_jobs": (int(unknown_jobs) / int(jobs)) if jobs else None}
    return {"intent": intent, "window": {"since": since, "as_of": as_of, "field": "ts"}, "totals": totals,
            "by_executor_lane": by_executor_lane, "by_served_model": by_served_model,
            "model_drift": model_drift, "fell": fell, "shares": shares, "samples": samples, "caveats": caveats}


def routing_summary(intent, *, since=None, as_of=None):
    """Compact advise block, derived from the same full-report aggregation."""
    report = route_realized_report(intent, since=since, as_of=as_of, limit=0)
    return {"window": report["window"], "by_executor_lane": report["by_executor_lane"],
            "shares": report["shares"]}


def cmd(rest):
    ap = argparse.ArgumentParser(prog="spendguard route-realized")
    ap.add_argument("--intent", required=True, help="exact calls.intent label")
    ap.add_argument("--since", help="inclusive lower ts bound (ISO date/timestamp string)")
    ap.add_argument("--as-of", dest="as_of", help="inclusive upper ts bound (ISO date/timestamp string)")
    ap.add_argument("--limit", type=int, default=20, help="maximum recent samples (default 20)")
    ap.add_argument("--json", action="store_true", help="emit the full report as JSON")
    a = ap.parse_args(rest)
    report = route_realized_report(a.intent, since=a.since, as_of=a.as_of, limit=a.limit)
    if a.json:
        print(json.dumps(report, indent=2, default=str))
        return 0
    totals = report["totals"]
    print(f"route-realized — intent {a.intent!r} · {totals['jobs']} job(s) · billed ${totals['billed_cost']:.6f}")
    print(f"  {'EXECUTOR':<16} {'LANE':<16} {'JOBS':>6} {'BILLED':>12} {'$0 JOBS':>9}")
    for row in report["by_executor_lane"]:
        print(f"  {row['executor']:<16.16} {row['resolved_lane']:<16.16} {row['jobs']:>6} "
              f"${row['billed_cost']:>10.6f} {row['zero_cost_jobs']:>9}")
    for caveat in report["caveats"]:
        print(f"  caveat: {caveat}")
    return 0
