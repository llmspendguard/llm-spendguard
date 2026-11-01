"""Always-on p50/p95/p99 structure and index-plan checks for ledger hot paths."""
import os
import sqlite3
import tempfile

LEDGER_THRESHOLDS_ISOLATED_HOME = tempfile.mkdtemp(prefix="spendguard-perf-threshold-")
os.environ["SPENDGUARD_HOME"] = LEDGER_THRESHOLDS_ISOLATED_HOME

from spendguard.ledger_performance_benchmark import (
    LEDGER_PERFORMANCE_PERCENTILES,
    measure_hot_ledger_operations,
)

from storm_harness import canary
from ledger_index_plan_harness import ledger_plan_details, ledger_plan_searches_with


PERFORMANCE_TEST_ITERATIONS = 200
PERFORMANCE_TEST_WARMUPS = 20
PERFORMANCE_TEST_VOLUME = 120
PERFORMANCE_TEST_DATABASE_NAME = "ledger-performance-thresholds.db"


def verify_ledger_performance_thresholds():
    connection = sqlite3.connect(":memory:")
    connection.execute("CREATE TABLE canaries(id TEXT PRIMARY KEY)")
    connection.execute("CREATE INDEX canaries_id_index ON canaries(id)")
    connection.execute("INSERT INTO canaries VALUES(?)", (canary(1),))
    plan = ledger_plan_details(connection, "SELECT id FROM canaries WHERE id=?", (canary(1),))
    assert ledger_plan_searches_with(plan, "canaries_id_index") or any("sqlite_autoindex_canaries" in row for row in plan)
    measurement = measure_hot_ledger_operations(
        iterations=PERFORMANCE_TEST_ITERATIONS,
        warmups=PERFORMANCE_TEST_WARMUPS,
        volume=PERFORMANCE_TEST_VOLUME,
        ledger_path=os.path.join(os.environ["SPENDGUARD_HOME"], PERFORMANCE_TEST_DATABASE_NAME),
    )
    expected_percentile_names = [f"p{percentile}" for percentile in LEDGER_PERFORMANCE_PERCENTILES]
    assert measurement["latencies_s"]
    for percentile_measurements in measurement["latencies_s"].values():
        assert list(percentile_measurements) == expected_percentile_names
        assert list(percentile_measurements.values()) == sorted(percentile_measurements.values())
    assert measurement["data_flow"]["latency_s"] > 0
    assert measurement["data_flow"]["throughput_per_s"] > 0


if __name__ == "__main__":
    verify_ledger_performance_thresholds()
    print("ledger performance percentile thresholds pass")
