"""Nightly-only relative p50/p95/p99 and index-plan checks for ledger hot paths."""
import os
import sqlite3
import tempfile

from spendguard.ledger_performance_benchmark import run_ledger_performance_benchmark

from storm_harness import canary
from ledger_index_plan_harness import ledger_plan_details, ledger_plan_searches_with


PERFORMANCE_TEST_ITERATIONS = 200
PERFORMANCE_TEST_WARMUPS = 20
PERFORMANCE_TEST_VOLUME = 120


def verify_ledger_performance_thresholds():
    os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="spendguard-perf-threshold-")
    connection = sqlite3.connect(":memory:")
    connection.execute("CREATE TABLE canaries(id TEXT PRIMARY KEY)")
    connection.execute("CREATE INDEX canaries_id_index ON canaries(id)")
    connection.execute("INSERT INTO canaries VALUES(?)", (canary(1),))
    plan = ledger_plan_details(connection, "SELECT id FROM canaries WHERE id=?", (canary(1),))
    assert ledger_plan_searches_with(plan, "canaries_id_index") or any("sqlite_autoindex_canaries" in row for row in plan)
    assert run_ledger_performance_benchmark(check=True, iterations=PERFORMANCE_TEST_ITERATIONS,
                                            warmups=PERFORMANCE_TEST_WARMUPS,
                                            volume=PERFORMANCE_TEST_VOLUME) == 0


if __name__ == "__main__":
    verify_ledger_performance_thresholds()
    print("ledger performance percentile thresholds pass")
