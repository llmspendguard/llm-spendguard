"""Offline, opt-in performance measurements for the ledger's production-shaped hot paths."""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta, timezone
from importlib.resources import files
import json
import os
from pathlib import Path
import platform
import sqlite3
import sys
import time

from . import config
from .ledger import SpendLedger


LEDGER_PERFORMANCE_BASELINE_RESOURCE = "ledger_performance_baseline.json"
LEDGER_PERFORMANCE_SOURCE_LEDGER_RESOURCE = "spend.db"
LEDGER_PERFORMANCE_SCHEMA_VERSION = 1
LEDGER_PERFORMANCE_DEFAULT_ITERATIONS = 80
LEDGER_PERFORMANCE_DEFAULT_WARMUPS = 10
LEDGER_PERFORMANCE_DEFAULT_VOLUME = 300
LEDGER_PERFORMANCE_PERCENTILES = (50, 95, 99)
LEDGER_PERFORMANCE_FALLBACK_PROFILE_SOURCE = "documented-synthetic-metadata"
LEDGER_PERFORMANCE_RUNNER_ENV = "SPENDGUARD_BENCH_RUNNER_CLASS"
LEDGER_PERFORMANCE_ACTION_COUNT = 2
LEDGER_PERFORMANCE_PROVIDER_CARDINALITY = 5
LEDGER_PERFORMANCE_MODEL_CARDINALITY = 11
LEDGER_PERFORMANCE_INTENT_CARDINALITY = 31
LEDGER_PERFORMANCE_CONVERSATION_CARDINALITY = 7
LEDGER_PERFORMANCE_FIXTURE_TIMESTAMP = "2026-10-01T00:00:00+00:00"
LEDGER_PERFORMANCE_FIXTURE_DAY = "2026-10-01"
LEDGER_PERFORMANCE_FIXTURE_PERIOD = "2026-10"
LEDGER_PERFORMANCE_FIXTURE_COST = "0.000001"
LEDGER_PERFORMANCE_FIXTURE_COST_NANO = 1000
LEDGER_PERFORMANCE_SUCCESS_EXIT = 0
LEDGER_PERFORMANCE_REGRESSION_EXIT = 1
LEDGER_PERFORMANCE_METADATA_SOURCE = "ledger-metadata"


def calculate_ledger_latency_percentiles(samples):
    """Return nearest-rank percentiles from complete samples; no observation is discarded."""
    ordered = sorted(samples)
    if not ordered:
        raise ValueError("latency samples must not be empty")
    return {
        f"p{percentile}": ordered[min(len(ordered) - 1, (percentile * len(ordered) - 1) // 100)]
        for percentile in LEDGER_PERFORMANCE_PERCENTILES
    }


def derive_retrospective_ledger_load_profile(connection, fallback_volume):
    """Derive traffic metadata from spend_events/calls, never prompt or response content."""
    tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    events = []
    calls = []
    if "spend_events" in tables:
        events = connection.execute(
            "SELECT occurred_at,provider,model,intent,in_tok,out_tok FROM spend_events ORDER BY occurred_at"
        ).fetchall()
    if "calls" in tables:
        call_columns = {row[1] for row in connection.execute("PRAGMA table_info(calls)")}
        required = {"ts", "provider", "model", "intent", "in_tok", "out_tok", "latency"}
        if required.issubset(call_columns):
            calls = connection.execute(
                "SELECT ts,provider,model,intent,in_tok,out_tok,latency FROM calls ORDER BY ts"
            ).fetchall()
    combined = [tuple(row[:6]) for row in events] + [tuple(row[:6]) for row in calls]
    if not combined:
        return {
            "source": LEDGER_PERFORMANCE_FALLBACK_PROFILE_SOURCE,
            "volume": fallback_volume,
            "call_mix": {"realtime": fallback_volume},
            "token_sizes": [1],
            "arrival_gaps_s": [0.0],
        }
    timestamps = sorted(
        datetime.fromisoformat(str(row[0]).replace("Z", "+00:00")) for row in combined if row[0]
    )
    gaps = [(right - left).total_seconds() for left, right in zip(timestamps, timestamps[1:])]
    mix = Counter(str(row[2] or row[1] or "unknown") for row in combined)
    sizes = [int(row[4] or 0) + int(row[5] or 0) for row in combined]
    return {"source": LEDGER_PERFORMANCE_METADATA_SOURCE, "volume": len(combined),
            "call_mix": dict(sorted(mix.items())),
            "token_sizes": sizes, "arrival_gaps_s": gaps or [0.0]}


def _ledger_performance_runner_class():
    configured = os.environ.get(LEDGER_PERFORMANCE_RUNNER_ENV)
    if configured:
        return configured
    return "-".join((platform.system(), platform.machine(), platform.python_implementation(),
                     f"{sys.version_info.major}.{sys.version_info.minor}"))


def _ledger_performance_queries():
    return {
        "event_by_id": ("SELECT 1 FROM spend_events WHERE id=?", ("missing",)),
        "money_reconcile": ("SELECT id FROM spend_events WHERE source='gate' AND cost_type='batch' AND "
                            "cost_basis='estimate' AND provider=? AND model=? AND intent IS ? "
                            "ORDER BY rowid DESC LIMIT 1", ("provider-1", "model-1", "intent-1")),
        "latest_source_conversation": ("SELECT conv_id FROM spend_events WHERE source=? "
                                       "ORDER BY occurred_at DESC,conv_id DESC LIMIT 1", ("gate",)),
        "source_conversation_stream": ("SELECT rowid,model,cache_read_tok FROM spend_events WHERE source=? "
                                       "AND conv_id=? ORDER BY occurred_at,rowid", ("gate", "conversation-1")),
        "recent_source_events": ("SELECT id FROM spend_events WHERE source=? AND occurred_at>=? "
                                 "ORDER BY occurred_at DESC", ("gate", "2000-01-01T00:00:00+00:00")),
        "provider_model_events": ("SELECT id FROM spend_events WHERE provider=? AND model=? ORDER BY rowid DESC LIMIT 1",
                                  ("provider-1", "model-1")),
        "intent_events": ("SELECT id FROM spend_events WHERE intent=? ORDER BY rowid DESC LIMIT 1", ("intent-1",)),
        "period_events": ("SELECT id FROM spend_events WHERE period=? ORDER BY occurred_at", ("2026-10",)),
        "status_events": ("SELECT id FROM spend_events WHERE status=? ORDER BY recorded_at DESC LIMIT 1", ("draft",)),
    }


def _populate_ledger_performance_fixture(ledger, load_profile):
    model_sequence = tuple(
        model
        for model, count in load_profile["call_mix"].items()
        for _ in range(count)
    )
    token_sizes = load_profile["token_sizes"]
    arrival_gaps = load_profile["arrival_gaps_s"]
    occurred_at = datetime.fromisoformat(LEDGER_PERFORMANCE_FIXTURE_TIMESTAMP)
    rows = []
    for number in range(load_profile["volume"]):
        if number:
            occurred_at += timedelta(seconds=arrival_gaps[(number - 1) % len(arrival_gaps)])
        timestamp = occurred_at.isoformat()
        rows.append((f"fixture-{number}", timestamp, timestamp, timestamp, "UTC", timestamp,
             LEDGER_PERFORMANCE_FIXTURE_DAY, LEDGER_PERFORMANCE_FIXTURE_PERIOD,
             "gate", "batch" if number % LEDGER_PERFORMANCE_ACTION_COUNT else "realtime", "estimate",
             f"provider-{number % LEDGER_PERFORMANCE_PROVIDER_CARDINALITY}",
             model_sequence[number % len(model_sequence)],
             f"intent-{number % LEDGER_PERFORMANCE_INTENT_CARDINALITY}",
             f"conversation-{number % LEDGER_PERFORMANCE_CONVERSATION_CARDINALITY}", "draft", "USD",
             token_sizes[number % len(token_sizes)], 0,
             LEDGER_PERFORMANCE_FIXTURE_COST, LEDGER_PERFORMANCE_FIXTURE_COST_NANO))
    ledger._conn.executemany(
        "INSERT INTO spend_events(id,ts_utc,recorded_at,occurred_at,tz,local_datetime,day,period,source,cost_type,"
        "cost_basis,provider,model,intent,conv_id,status,currency,in_tok,out_tok,realtime_usd,realtime_usd_nano) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    ledger._conn.commit()
    ledger._conn.execute("ANALYZE")


def measure_hot_ledger_operations(*, iterations, warmups, volume, ledger_path, retrospective_connection=None):
    """Measure complete latency series at production SQL and SpendLedger.record_event boundaries."""
    ledger = SpendLedger(db_path=str(ledger_path), owned_connection=config.open_ledger_connection(str(ledger_path)))
    load_profile = derive_retrospective_ledger_load_profile(
        retrospective_connection or ledger._conn, volume
    )
    _populate_ledger_performance_fixture(ledger, load_profile)
    measurements = {}
    for name, (statement, parameters) in _ledger_performance_queries().items():
        samples = []
        for iteration in range(iterations + warmups):
            started = time.perf_counter()
            ledger._conn.execute(statement, parameters).fetchall()
            elapsed = time.perf_counter() - started
            if iteration >= warmups:
                samples.append(elapsed)
        measurements[name] = calculate_ledger_latency_percentiles(samples)
    write_samples = []
    for iteration in range(iterations + warmups):
        event_number = load_profile["volume"] + iteration
        started = time.perf_counter()
        ledger.record_event({"id": f"bench-{event_number}", "source": "bench", "kind": "realtime",
                             "usd": LEDGER_PERFORMANCE_FIXTURE_COST, "model": None, "intent": "benchmark"})
        elapsed = time.perf_counter() - started
        if iteration >= warmups:
            write_samples.append(elapsed)
    measurements["record_event"] = calculate_ledger_latency_percentiles(write_samples)
    flow_started = time.perf_counter()
    flow_count = ledger._conn.execute("SELECT COUNT(*) FROM spend_events WHERE source IN (?,?)", ("gate", "bench")).fetchone()[0]
    reconciled = ledger._conn.execute("SELECT COUNT(*) FROM spend_events WHERE id LIKE ?", ("bench-%",)).fetchone()[0]
    flow_elapsed = time.perf_counter() - flow_started
    ledger._conn.close()
    return {"latencies_s": measurements, "load_profile": load_profile,
            "data_flow": {"latency_s": flow_elapsed, "throughput_per_s": (flow_count + reconciled) / flow_elapsed}}


def _open_retrospective_ledger_metadata_connection(ledger_path):
    if not ledger_path.is_file():
        return None
    return sqlite3.connect(f"file:{ledger_path}?mode=ro", uri=True)


def compare_ledger_performance_baseline(current, baseline, tolerance):
    """Compare all metrics relative to the same-runner committed measurement."""
    regressions = []
    for path, percentiles in current["latencies_s"].items():
        for percentile, value in percentiles.items():
            limit = baseline["latencies_s"][path][percentile] * tolerance["latency_multiplier"]
            if value > limit:
                regressions.append(f"{path}.{percentile}: {value:.6f}s > {limit:.6f}s")
    latency_limit = baseline["data_flow"]["latency_s"] * tolerance["latency_multiplier"]
    throughput_floor = baseline["data_flow"]["throughput_per_s"] / tolerance["throughput_divisor"]
    if current["data_flow"]["latency_s"] > latency_limit:
        regressions.append("data_flow.latency_s")
    if current["data_flow"]["throughput_per_s"] < throughput_floor:
        regressions.append("data_flow.throughput_per_s")
    return regressions


def _print_ledger_performance_report(measurement):
    for path, values in sorted(measurement["latencies_s"].items()):
        print(path, " ".join(f"{name}={value * 1000:.3f}ms" for name, value in values.items()))
    flow = measurement["data_flow"]
    print(f"data_flow latency={flow['latency_s'] * 1000:.3f}ms throughput={flow['throughput_per_s']:.1f}/s")


def run_ledger_performance_benchmark(*, record=False, check=False, iterations=LEDGER_PERFORMANCE_DEFAULT_ITERATIONS,
                                     warmups=LEDGER_PERFORMANCE_DEFAULT_WARMUPS,
                                     volume=LEDGER_PERFORMANCE_DEFAULT_VOLUME):
    """Record or check the versioned benchmark for the current named runner class."""
    if record == check:
        raise ValueError("choose exactly one of --record or --check")
    if min(iterations, volume) <= 0 or warmups < 0:
        raise ValueError("iterations and volume must be positive; warmups must be non-negative")
    resource = Path(str(files(__package__).joinpath(LEDGER_PERFORMANCE_BASELINE_RESOURCE)))
    baseline = json.loads(resource.read_text(encoding="utf-8"))
    runner_class = _ledger_performance_runner_class()
    benchmark_home = config.active_spendguard_home()
    benchmark_home.mkdir(parents=True, exist_ok=True)
    ledger_path = benchmark_home / "ledger-performance-benchmark.db"
    if ledger_path.exists():
        ledger_path.unlink()
    source_ledger_path = benchmark_home / LEDGER_PERFORMANCE_SOURCE_LEDGER_RESOURCE
    retrospective_connection = _open_retrospective_ledger_metadata_connection(source_ledger_path)
    try:
        measurement = measure_hot_ledger_operations(
            iterations=iterations, warmups=warmups, volume=volume, ledger_path=ledger_path,
            retrospective_connection=retrospective_connection,
        )
    finally:
        if retrospective_connection is not None:
            retrospective_connection.close()
    measurement["provenance"] = {"runner_class": runner_class, "interpreter": sys.version,
                                 "platform": platform.platform(), "cpu": platform.processor() or platform.machine(),
                                 "recorded_at": datetime.now(timezone.utc).isoformat(),
                                 "iterations": iterations, "warmups": warmups, "volume": volume}
    _print_ledger_performance_report(measurement)
    if record:
        def update_ledger_performance_baseline(data):
            data["schema_version"] = LEDGER_PERFORMANCE_SCHEMA_VERSION
            data.setdefault("runner_classes", {})[runner_class] = measurement
        config.update_json(resource, update_ledger_performance_baseline, reason="ledger performance baseline recording",
                           keep_backups=0, required=True)
        print(f"recorded runner_class={runner_class} profile={measurement['load_profile']['source']}")
        return LEDGER_PERFORMANCE_SUCCESS_EXIT
    recorded = baseline.get("runner_classes", {}).get(runner_class)
    if recorded is None:
        print(f"no committed baseline for runner_class={runner_class}; run --record", file=sys.stderr)
        return LEDGER_PERFORMANCE_REGRESSION_EXIT
    regressions = compare_ledger_performance_baseline(measurement, recorded, baseline["tolerance"])
    print("PASS" if not regressions else "REGRESSION: " + "; ".join(regressions))
    return LEDGER_PERFORMANCE_SUCCESS_EXIT if not regressions else LEDGER_PERFORMANCE_REGRESSION_EXIT
