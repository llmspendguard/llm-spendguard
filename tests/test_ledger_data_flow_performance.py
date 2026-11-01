"""Nightly-only ingest -> record -> read -> reconcile performance coverage."""
import os
import sqlite3
import tempfile

from spendguard.ledger_performance_benchmark import (
    LEDGER_PERFORMANCE_FALLBACK_PROFILE_SOURCE,
    derive_retrospective_ledger_load_profile,
    run_ledger_performance_benchmark,
)

from storm_harness import CallerCollector, canary


DATA_FLOW_TEST_ITERATIONS = 200
DATA_FLOW_TEST_WARMUPS = 20
DATA_FLOW_TEST_VOLUME = 120


def verify_ledger_data_flow_performance():
    os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="spendguard-perf-flow-")
    profile = derive_retrospective_ledger_load_profile(sqlite3.connect(":memory:"), DATA_FLOW_TEST_VOLUME)
    assert profile["source"] == LEDGER_PERFORMANCE_FALLBACK_PROFILE_SOURCE
    collector = CallerCollector()
    collector.record({"status_code": 200, "text": "ok " + canary(1)})
    assert collector.bijection_ok([canary(1)])
    assert run_ledger_performance_benchmark(check=True, iterations=DATA_FLOW_TEST_ITERATIONS,
                                            warmups=DATA_FLOW_TEST_WARMUPS,
                                            volume=DATA_FLOW_TEST_VOLUME) == 0


if __name__ == "__main__":
    verify_ledger_data_flow_performance()
    print("ledger data-flow performance thresholds pass")
