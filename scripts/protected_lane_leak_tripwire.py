#!/usr/bin/env python3
"""Read-only tripwire for protected subscription-lane and metered-provider use."""
from __future__ import annotations

import argparse
from collections import defaultdict
from decimal import Decimal
from pathlib import Path
import sys

from spendguard.ledger_read import (
    LEDGER_CALLS_TABLE,
    RECENT_CALL_READ_COLUMNS,
    LedgerSQLReadRequest,
    read_ledger_sql,
)

CLEAN_EXIT = 0
LEAK_EXIT = 1
INCONCLUSIVE_EXIT = 2
PROBE_FAILED_EXIT = 3
DEFAULT_MIN_OTHER_CALLS = 1
HOURS_PER_DAY = Decimal("24")
ZERO_DOLLARS = Decimal("0")
API_EXECUTORS = (None, "", "api")
RECENT_PROTECTED_LANE_SQL = (
    f"SELECT {', '.join(RECENT_CALL_READ_COLUMNS)} FROM {LEDGER_CALLS_TABLE} "
    "WHERE datetime(ts) >= datetime('now', ?) ORDER BY datetime(ts)"
)


def protected_lane_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--protect-executor", required=True, help="subscription executor to protect")
    parser.add_argument("--protect-provider", required=True, help="metered API provider to protect")
    parser.add_argument("--hours", required=True, type=float, help="positive lookback window in hours")
    parser.add_argument("--min-other-calls", type=int, default=DEFAULT_MIN_OTHER_CALLS,
                        help="minimum other-lane calls required for CLEAN (default: %(default)s)")
    parser.add_argument("--ledger-path", type=Path,
                        help="ledger database path (default: spendguard's configured ledger)")
    return parser


def validated_protected_lane_arguments(argv: list[str] | None) -> argparse.Namespace:
    arguments = protected_lane_argument_parser().parse_args(argv)
    if arguments.hours <= 0:
        raise ValueError("--hours must be positive")
    if arguments.min_other_calls < 0:
        raise ValueError("--min-other-calls must be non-negative")
    return arguments


def protected_billing_groups(rows, protected_executor: str, protected_provider: str):
    metered_rows, plan_rows = [], []
    other_lane_markers = []
    for row in rows:
        executor = row["executor"]
        if executor == protected_executor:
            plan_rows.append(row)
        elif row["provider"] == protected_provider and executor in API_EXECUTORS:
            metered_rows.append(row)
        else:
            other_lane_markers.append(True)
    return metered_rows, plan_rows, len(other_lane_markers)


def summarized_protected_rows(rows):
    grouped = defaultdict(lambda: {"in_tok": 0, "num_calls": 0, "last_ts": None})
    for row in rows:
        key = (row["intent"], row["model"], row["executor"])
        summary = grouped[key]
        summary["in_tok"] += int(row["in_tok"] or 0)
        summary["num_calls"] += 1
        if summary["last_ts"] is None or row["ts"] > summary["last_ts"]:
            summary["last_ts"] = row["ts"]
    return grouped


def print_protected_group(label: str, rows) -> None:
    print(label)
    ordered_groups = sorted(summarized_protected_rows(rows).items(),
                            key=lambda item: tuple(str(value) for value in item[0]))
    for (intent, model, executor), summary in ordered_groups:
        print(f"  intent={intent!r}, model={model!r}, executor={executor!r}, "
              f"in_tok={summary['in_tok']}, num_calls={summary['num_calls']}, last_ts={summary['last_ts']}")


def print_leak_report(metered_rows, plan_rows, hours: float) -> None:
    metered_cost = sum((Decimal(str(row["cost"] or 0)) for row in metered_rows), ZERO_DOLLARS)
    if metered_rows:
        print_protected_group(f"METERED API — REAL DOLLARS (billed cost ${metered_cost:.6f})", metered_rows)
    if plan_rows:
        print_protected_group(
            "PLAN LANE — $0 in ledger but consumes the capped plan; a $0 plan-lane row is not free, it landed on the plan",
            plan_rows)
    daily_extrapolation = metered_cost / Decimal(str(hours)) * HOURS_PER_DAY
    print(f"Daily metered API extrapolation from --hours {hours:g} h: ${daily_extrapolation:.6f}/day")
    print(f"${metered_cost:.6f} = ${metered_cost:.6f} API + $0 Subscription + $0 Remote "
          ":: Est sub value $0 (not derivable from call-ledger rows)")


def run_protected_lane_probe(arguments: argparse.Namespace) -> int:
    request = LedgerSQLReadRequest(RECENT_PROTECTED_LANE_SQL, (f"-{arguments.hours:g} hours",))
    rows = read_ledger_sql(request, path=arguments.ledger_path)
    metered_rows, plan_rows, other_call_count = protected_billing_groups(
        rows, arguments.protect_executor, arguments.protect_provider)
    if metered_rows or plan_rows:
        print_leak_report(metered_rows, plan_rows, arguments.hours)
        return LEAK_EXIT
    if other_call_count < arguments.min_other_calls:
        print(f"INCONCLUSIVE: only {other_call_count} other-lane calls in the last {arguments.hours:g} h "
              "— too quiet to prove anything")
        return INCONCLUSIVE_EXIT
    print(f"CLEAN: protected lane untouched; {other_call_count} calls ran on other lanes "
          f"in the last {arguments.hours:g} h")
    return CLEAN_EXIT


def main(argv: list[str] | None = None) -> int:
    try:
        return run_protected_lane_probe(validated_protected_lane_arguments(argv))
    except SystemExit as error:
        if error.code == CLEAN_EXIT:
            return CLEAN_EXIT
        print(f"PROBE FAILED: invalid arguments (argparse exit {error.code})", file=sys.stderr)
        return PROBE_FAILED_EXIT
    except Exception as error:
        print(f"PROBE FAILED: {error}", file=sys.stderr)
        return PROBE_FAILED_EXIT


if __name__ == "__main__":
    raise SystemExit(main())
