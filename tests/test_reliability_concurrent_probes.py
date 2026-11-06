"""GUARD — reliability.sweep probes metered providers CONCURRENTLY, not serially. The serial loop made N raw-key
probes take up to N*timeout_s and OVERRAN the spendguard_health(run=true) MCP call window (a peer session hit this
on b4ddcd3, which added the raw-key metered_only sweep); concurrent, the sweep is bounded by the SLOWEST single
probe, so N providers fit the same window one does. Pins: (a) every provider is still probed with the right
result shape, (b) every probe rendezvous overlaps, with a generous wall-clock hang guard. Hermetic: adapters.call is stubbed to sleep; no
network, no spend, no ledger."""
import os
import sys
import tempfile
import time
import threading

os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="sg-relconc-")
os.environ.setdefault("SPENDGUARD_TEST_ISOLATED", "1")
os.environ.setdefault("SPENDGUARD_NO_AUTOINSTALL", "1")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from _script_checks import record_script_check
from spendguard import adapters, reliability, lanes   # noqa: E402

fails = []



N = 4
SLEEP = 0.3
PROBE_SCHEDULER_CUSHION = 100
PROBE_PROMPTNESS_SECONDS = SLEEP * PROBE_SCHEDULER_CUSHION
PROBE_HANG_MULTIPLE = 3
PROBE_SYNC_SECONDS = PROBE_PROMPTNESS_SECONDS * PROBE_HANG_MULTIPLE
probe_concurrent_start = threading.Barrier(N)
reliability.plan = lambda: {"estimate": {}, "lanes": [], "metered": [(f"prov{i}", "m") for i in range(N)]}
reliability.sweep_estimate = lambda pl=None: {}
lanes.probe = lambda timeout_s=None: []


def _slow_call(model, prompt, **kw):
    probe_concurrent_start.wait(PROBE_SYNC_SECONDS)
    time.sleep(SLEEP)                                     # each probe takes SLEEP; serial would be N*SLEEP
    return {"error": None, "executor": "api", "served_by_metered_api": True, "cost": 0.0001, "truncated": False}


adapters.call = _slow_call

t0 = time.monotonic()
res = reliability.sweep(run=True, timeout_s=5)
elapsed = time.monotonic() - t0

record_script_check(fails, f"all {N} metered providers were probed", len(res["metered"]) == N)
record_script_check(fails, "each carries the raw-key result shape (reachable/executor/latency)",
   all(set(res["metered"][f"prov{i}"]) >= {"reachable", "executor", "latency", "reason"} for i in range(N)))
record_script_check(fails, "all api-served → reachable (raw key verified)", all(res["metered"][f"prov{i}"]["reachable"] for i in range(N)))
record_script_check(fails, "all probes rendezvoused concurrently", not probe_concurrent_start.broken)
record_script_check(fails, "probes complete within the scheduling-tolerant hang guard",
   elapsed < PROBE_PROMPTNESS_SECONDS)

print(f"\n{'[FAIL]' if fails else 'OK'} test_reliability_concurrent_probes: {len(fails)} failure(s)")
sys.exit(1 if fails else 0)
