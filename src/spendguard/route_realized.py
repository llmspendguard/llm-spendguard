"""Read-only realized-routing history from the calls ledger. No calls, probes, or prices."""

from .capability_cli_parsing import add_registered_cli_argument

import argparse
import json
from pathlib import Path

from . import config, ledger_read


def route_realized_report(intent, *, since=None, as_of=None, limit=20):
    # An install with no call corpus has empty history; observing it must not initialize SQLite.
    if (not Path(config.db_path()).exists()
            or not ledger_read.ledger_read_table_exists(ledger_read.LEDGER_CALLS_TABLE)):
        return {"intent": intent, "window": {"since": since, "as_of": as_of, "field": "ts"},
                "totals": {"jobs": 0, "billed_cost": 0.0, "billed_jobs": 0,
                           "zero_cost_jobs": 0, "unknown_routing_jobs": 0},
                "by_executor_lane": [], "by_served_model": [], "model_drift": [],
                "fell": {"total_fell_jobs": 0, "by_fell_reason": [], "by_fell_from": [],
                         "by_redirect_reason": []},
                "shares": {"metered_share_jobs": None, "metered_share_cost": None,
                           "zero_lane_share_jobs": None, "unknown_share_jobs": None},
                "samples": [], "caveats": ["no calls found for this intent in the requested ts window"]}
    return ledger_read.read_routing_history_report(intent, since=since, as_of=as_of, limit=limit)


def routing_summary(intent, *, since=None, as_of=None):
    """Compact advise block, derived from the same full-report aggregation."""
    report = route_realized_report(intent, since=since, as_of=as_of, limit=0)
    return {"window": report["window"], "by_executor_lane": report["by_executor_lane"],
            "shares": report["shares"]}


def cmd(rest):
    ap = argparse.ArgumentParser(prog="spendguard route-realized")
    add_registered_cli_argument(ap, 'route-realized/route_realized.cmd/ap/--intent')
    add_registered_cli_argument(ap, 'route-realized/route_realized.cmd/ap/--since')
    add_registered_cli_argument(ap, 'route-realized/route_realized.cmd/ap/--as-of')
    add_registered_cli_argument(ap, 'route-realized/route_realized.cmd/ap/--limit', type=int)
    add_registered_cli_argument(ap, 'route-realized/route_realized.cmd/ap/--json')
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
