"""Nightly-only ingest -> record -> read -> reconcile performance coverage."""
import os
import sqlite3
import tempfile

from spendguard.ledger_performance_benchmark import (
    LEDGER_PERFORMANCE_FALLBACK_PROFILE_SOURCE,
    LEDGER_PERFORMANCE_METADATA_SOURCE,
    derive_retrospective_ledger_load_profile,
    measure_hot_ledger_operations,
    run_ledger_performance_benchmark,
)

from storm_harness import CallerCollector, canary


DATA_FLOW_TEST_ITERATIONS = 200
DATA_FLOW_TEST_WARMUPS = 20
DATA_FLOW_TEST_VOLUME = 120
RETROSPECTIVE_FIXTURE_DATABASE_NAME = "benchmark.db"
RETROSPECTIVE_CONSUMPTION_ITERATIONS = 1
RETROSPECTIVE_CONSUMPTION_WARMUPS = 0
RETROSPECTIVE_EVENT_ROWS = (
    ("event-one", "2026-10-01T00:00:00+00:00", "provider-a", "model-a", "intent-a", 10, 2),
    ("event-two", "2026-10-01T00:00:03+00:00", "provider-a", "model-a", "intent-b", 20, 4),
)
RETROSPECTIVE_CALL_ROWS = (
    ("call-one", "2026-10-01T00:00:08+00:00", "provider-b", "model-b", "intent-c", 30, 6, 0.2),
)


def create_representative_retrospective_connection():
    connection = sqlite3.connect(":memory:")
    connection.execute(
        "CREATE TABLE spend_events(id TEXT PRIMARY KEY, occurred_at TEXT, provider TEXT, model TEXT, "
        "intent TEXT, in_tok INTEGER, out_tok INTEGER)"
    )
    connection.execute(
        "CREATE TABLE calls(id TEXT PRIMARY KEY, ts TEXT, provider TEXT, model TEXT, intent TEXT, "
        "in_tok INTEGER, out_tok INTEGER, latency REAL)"
    )
    connection.executemany("INSERT INTO spend_events VALUES(?,?,?,?,?,?,?)", RETROSPECTIVE_EVENT_ROWS)
    connection.executemany("INSERT INTO calls VALUES(?,?,?,?,?,?,?,?)", RETROSPECTIVE_CALL_ROWS)
    return connection


def verify_ledger_data_flow_performance():
    os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="spendguard-perf-flow-")
    profile = derive_retrospective_ledger_load_profile(sqlite3.connect(":memory:"), DATA_FLOW_TEST_VOLUME)
    assert profile["source"] == LEDGER_PERFORMANCE_FALLBACK_PROFILE_SOURCE
    retrospective_connection = create_representative_retrospective_connection()
    retrospective_profile = derive_retrospective_ledger_load_profile(
        retrospective_connection, DATA_FLOW_TEST_VOLUME
    )
    assert retrospective_profile == {
        "source": LEDGER_PERFORMANCE_METADATA_SOURCE,
        "volume": len(RETROSPECTIVE_EVENT_ROWS) + len(RETROSPECTIVE_CALL_ROWS),
        "call_mix": {"model-a": 2, "model-b": 1},
        "token_sizes": [12, 24, 36],
        "arrival_gaps_s": [3.0, 5.0],
    }
    fixture_directory = tempfile.mkdtemp(prefix="spendguard-retrospective-fixture-")
    fixture_path = os.path.join(fixture_directory, RETROSPECTIVE_FIXTURE_DATABASE_NAME)
    measurement = measure_hot_ledger_operations(
        iterations=RETROSPECTIVE_CONSUMPTION_ITERATIONS,
        warmups=RETROSPECTIVE_CONSUMPTION_WARMUPS,
        volume=DATA_FLOW_TEST_VOLUME,
        ledger_path=fixture_path,
        retrospective_connection=retrospective_connection,
    )
    assert measurement["load_profile"] == retrospective_profile
    fixture_connection = sqlite3.connect(fixture_path)
    populated_models = dict(
        fixture_connection.execute(
            "SELECT model, COUNT(*) FROM spend_events WHERE source='gate' GROUP BY model"
        ).fetchall()
    )
    populated_tokens = [
        row[0]
        for row in fixture_connection.execute(
            "SELECT in_tok + out_tok FROM spend_events WHERE source='gate' ORDER BY rowid"
        ).fetchall()
    ]
    assert populated_models == retrospective_profile["call_mix"]
    assert populated_tokens == retrospective_profile["token_sizes"]
    fixture_connection.close()
    retrospective_connection.close()
    collector = CallerCollector()
    collector.record({"status_code": 200, "text": "ok " + canary(1)})
    assert collector.bijection_ok([canary(1)])
    assert run_ledger_performance_benchmark(check=True, iterations=DATA_FLOW_TEST_ITERATIONS,
                                            warmups=DATA_FLOW_TEST_WARMUPS,
                                            volume=DATA_FLOW_TEST_VOLUME) == 0


if __name__ == "__main__":
    verify_ledger_data_flow_performance()
    print("ledger data-flow performance thresholds pass")
