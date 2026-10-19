import spendguard; spendguard.require()

"""Read-only Codex demand: observed agentic events and separate ledger reconstruction."""
import argparse
import collections
import datetime
import json
import math
from pathlib import Path
import sqlite3

from spendguard import config
from spendguard.delegate_router import CODEX_AGENTIC_EVENTS_SCHEMA_VERSION, codex_agentic_events_path

CODEX_DEMAND_LANE = "codex"
CODEX_DEMAND_PERCENTILES = (50, 95)
CODEX_DEMAND_SECONDS_PER_MINUTE = 60


def codex_demand_weighted_quantile(histogram, percentile):
    if not sum(histogram.values()):
        return None
    target = sum(histogram.values()) * percentile / 100
    cumulative = 0
    for concurrency, weight in sorted(histogram.items()):
        cumulative += weight
        if cumulative >= target:
            return concurrency
    return None


def codex_demand_parse_timestamp(value):
    parsed = datetime.datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError("timestamp must include timezone")
    return parsed.astimezone(datetime.timezone.utc)


def codex_demand_interval_summary(intervals, since, until, pool_size):
    """Exact half-open sweep, weighted by elapsed wall time, including idle time."""
    if until <= since or pool_size < 1:
        raise ValueError("positive window and pool size required")
    events = collections.Counter()
    included = 0
    for start, end in intervals:
        if end < start:
            raise ValueError("execution ended before it started")
        start, end = max(start, since), min(end, until)
        if start < end:
            events[start] += 1
            events[end] -= 1
            included += 1
    histogram = collections.Counter()
    timeline = []
    previous, concurrency = since, 0
    for timestamp, delta in sorted(events.items()):
        if timestamp > previous:
            histogram[concurrency] += (timestamp - previous).total_seconds()
            timeline.append({"since": previous.isoformat(), "until": timestamp.isoformat(),
                             "concurrency": concurrency})
        concurrency += delta
        previous = timestamp
    if previous < until:
        histogram[concurrency] += (until - previous).total_seconds()
        timeline.append({"since": previous.isoformat(), "until": until.isoformat(),
                         "concurrency": concurrency})
    active = {key: seconds for key, seconds in histogram.items() if key > 0}
    duration = (until - since).total_seconds()
    return {
        "usable_intervals": included, "timeline": timeline,
        "time_weighted_all_window": {f"p{p}": codex_demand_weighted_quantile(histogram, p)
                                     for p in CODEX_DEMAND_PERCENTILES},
        "time_weighted_active_only": {f"p{p}": codex_demand_weighted_quantile(active, p)
                                      for p in CODEX_DEMAND_PERCENTILES},
        "max_concurrent": max(histogram, default=0),
        "seconds_by_concurrency": dict(sorted(histogram.items())),
        "saturation_fraction": sum(seconds for count, seconds in histogram.items()
                                   if count >= pool_size) / duration,
    }


def codex_demand_read_agentic_events(path, since, until, pool_size):
    """Read the entire journal, pairing IDs rather than guessing from ledger latency.

    Unfinished executions are named gaps, never assigned a fabricated completion.
    The observed summary is a lower bound when the window lacks full coverage.
    """
    pairs = collections.defaultdict(dict)
    gaps = collections.Counter()
    coverage = []
    try:
        with Path(path).open() as source:
            for line in source:
                try:
                    event = json.loads(line)
                    timestamp = codex_demand_parse_timestamp(event["ts"])
                    if event.get("schema_version") != CODEX_AGENTIC_EVENTS_SCHEMA_VERSION:
                        raise ValueError("unsupported schema")
                    kind = event["event"]
                    if kind == "coverage_start":
                        coverage.append(timestamp)
                    elif kind in ("start", "end"):
                        identity = event["execution_id"]
                        if kind in pairs[identity]:
                            gaps["duplicate_event"] += 1
                        pairs[identity][kind] = timestamp
                    else:
                        raise ValueError("unknown event")
                except (ValueError, TypeError, KeyError):
                    gaps["invalid_event"] += 1
    except FileNotFoundError:
        gaps["event_journal_missing"] += 1
    intervals = []
    for pair in pairs.values():
        start, end = pair.get("start"), pair.get("end")
        if start is None:
            if end >= since:
                gaps["end_without_start"] += 1
        elif end is None:
            if start < until:
                gaps["start_without_end"] += 1
        elif end < start:
            gaps["negative_interval"] += 1
        elif start < until and end > since:
            intervals.append((start, end))
    capture_start = min(coverage) if coverage else None
    if capture_start is None or capture_start > since:
        gaps["window_predates_capture"] += 1
    complete = not gaps
    observed = codex_demand_interval_summary(intervals, since, until, pool_size) if intervals or complete else None
    if not complete:
        verdict = "insufficient evidence: historical or incomplete capture cannot establish pool sizing"
    elif observed["max_concurrent"] > pool_size:
        verdict = "under observed peak demand; demand exceeded warm slots (cold CLI fallback can exceed the pool)"
    elif observed["max_concurrent"] == pool_size:
        verdict = "matches observed peak demand; saturation fraction reports time at the cap"
    else:
        verdict = "above observed agentic peak; this window alone does not establish spare capacity for all lane traffic"
    return {"source": str(path), "configured_pool_size": pool_size,
            "capture_since": capture_start.isoformat() if capture_start else None,
            "complete_window": complete, "gaps": dict(gaps), "observed": observed,
            "completeness_scope": "paired journal records only; deployment coverage must be verified separately",
            "pool_size_verdict": verdict,
            "scope": "delegate_router -> codex_exec run_prompt, including queue/warm/cold/fallback; not provider turns",
            "coverage_caveat": "coverage_start marks journal creation, not proof old uninstrumented processes have drained"}


def measure_codex_demand_from_ledger():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=float, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--database", default=config.db_path())
    parser.add_argument("--until", type=codex_demand_parse_timestamp,
                        help="fixed ISO timestamp with timezone for reproducible windows")
    parser.add_argument("--agentic-events", type=Path, default=codex_agentic_events_path())
    args = parser.parse_args()
    if args.days <= 0:
        parser.error("--days must be positive")
    now = args.until or datetime.datetime.now(datetime.timezone.utc)
    since = now - datetime.timedelta(days=args.days)
    connection = sqlite3.connect(Path(args.database).resolve().as_uri() + "?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    columns = [row[1] for row in connection.execute("PRAGMA table_info(calls)")]
    required = {"id", "ts", "latency", "executor", "resolved_lane", "kind", "intent", "caller", "model"}
    if not required.issubset(columns):
        raise RuntimeError(f"Missing columns: {required.difference(columns)}")
    rows = connection.execute(
        "SELECT id,ts,latency,executor,resolved_lane,kind,intent,caller,model,outcome FROM calls "
        "WHERE executor=? OR resolved_lane=? ORDER BY ts",
        (CODEX_DEMAND_LANE, CODEX_DEMAND_LANE))
    sample_record = None
    events = collections.Counter()
    minute_counts = collections.Counter()
    intent_counts = collections.Counter()
    gaps = collections.Counter()
    included = 0
    for row in rows:
        try:
            end = datetime.datetime.fromisoformat(row["ts"])
            if end.tzinfo is None:
                gaps["timestamp_without_timezone"] += 1
                continue
        except (ValueError, TypeError):
            gaps["invalid_timestamp"] += 1
            continue
        if end < since:
            continue
        if end <= now:
            sample_record = dict(row)
            minute_counts[end.replace(second=0, microsecond=0).isoformat()] += 1
            intent_counts[row["intent"]] += 1
        latency = row["latency"]
        if not isinstance(latency, (int, float)) or not math.isfinite(latency) or latency <= 0:
            if end <= now:
                gaps["recent_missing_or_nonpositive_latency"] += 1
            continue
        start = max(since, end - datetime.timedelta(seconds=latency))
        end = min(end, now)
        if start >= end:
            continue
        events[start] += 1
        events[end] -= 1
        included += 1
    histogram = collections.Counter()
    arrival_histogram = collections.Counter()
    concurrency = 0
    previous = since
    for timestamp, delta in sorted(events.items()):
        histogram[concurrency] += (timestamp - previous).total_seconds()
        concurrency += delta
        if delta > 0:
            arrival_histogram[concurrency] += delta
        previous = timestamp
    histogram[concurrency] += (now - previous).total_seconds()
    active_histogram = {key: value for key, value in histogram.items() if key > 0}
    decisions = list(connection.execute(
        "SELECT ts,intent,chosen_model FROM decisions WHERE basis=? ORDER BY ts",
        ("delegate",)))
    decisions = [row for row in decisions if since <= codex_demand_parse_timestamp(row["ts"]) <= now]
    decision_minutes = collections.Counter(row["ts"][:16] for row in decisions)
    report = {
        "window": {"since": since.isoformat(), "until": now.isoformat(), "days": args.days},
        "verified_columns": columns, "sample_record_before_aggregation": sample_record,
        "selected_recent_calls": sum(minute_counts.values()), "usable_intervals": included,
        "gaps": dict(gaps), "intent_counts": dict(intent_counts),
        "time_weighted_all_window": {f"p{p}": codex_demand_weighted_quantile(histogram, p) for p in CODEX_DEMAND_PERCENTILES},
        "time_weighted_active_only": {f"p{p}": codex_demand_weighted_quantile(active_histogram, p) for p in CODEX_DEMAND_PERCENTILES},
        "positive_net_event_weighted": {f"p{p}": codex_demand_weighted_quantile(arrival_histogram, p) for p in CODEX_DEMAND_PERCENTILES},
        "max_observed_reconstructed_concurrency": max(active_histogram, default=0),
        "seconds_by_concurrency": dict(sorted(histogram.items())),
        "completion_counts_per_active_minute": dict(sorted(minute_counts.items())),
        "all_lane_delegate_decisions_count": len(decisions),
        "all_lane_delegate_decisions_max_per_minute": max(decision_minutes.values(), default=0),
        "agentic": codex_demand_read_agentic_events(args.agentic_events, since, now,
                                                   config.codex_daemon_pool_settings()["size"]),
        "ledger_reconstruction_scope": "non-agentic lane call latency reconstruction, separate from agentic journal",
        "methodology_caveats": [
            "Observed lane calls use executor=codex OR resolved_lane=codex; includes all recorded intents, not only delegation.",
            "calls.ts is completion time rounded to seconds; start=ts-latency is reconstructed, not independently observed.",
            "Latency includes queue, setup and completion, so this is demand/service-path overlap, not provider-turn overlap.",
            "Half-open intervals; simultaneous completion/start events are combined. Positive-net-event quantiles are not exact arrival quantiles.",
            "Agentic overlap is measured separately from durable paired start/end journal events; unavailable historical signal is not zero demand.",
            "Decision counts include all lanes because decisions has no explicit executor; no semantic lane inference was applied.",
            "No historical calls end timestamp exists separately from completion ts; rows without positive latency are excluded and named as gaps.",
        ],
    }
    connection.close()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({key: report[key] for key in ("selected_recent_calls", "usable_intervals", "gaps", "time_weighted_all_window", "time_weighted_active_only", "positive_net_event_weighted", "max_observed_reconstructed_concurrency", "all_lane_delegate_decisions_count")}))
    agentic = report["agentic"]
    observed = agentic["observed"]
    print(json.dumps({"agentic": {"p50_p95": observed["time_weighted_all_window"] if observed else None,
                                  "max": observed["max_concurrent"] if observed else None,
                                  "saturation_fraction": observed["saturation_fraction"] if observed else None,
                                  "N": agentic["configured_pool_size"], "gaps": agentic["gaps"],
                                  "verdict": agentic["pool_size_verdict"]}}))


if __name__ == "__main__":
    measure_codex_demand_from_ledger()
