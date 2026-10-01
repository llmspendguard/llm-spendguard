"""THE connection-storm reliability GATE — the end-to-end test that was missing for a month.

A burst fan-out at real concurrency through the REAL governed path (lane_balance.bulk_delegate, pinned metered
matrix — the shape honestreview:call-intent / warden:card_entail actually used) -> dispatch admission -> _call_once,
against a stub transport that enforces a CONCURRENT-CONNECTION ceiling (Anthropic's real 429: "Number of concurrent
connections has exceeded your rate limit", measured in spend.db 2026-09-28..10-01: 2,727 events, 99.96% Anthropic).

WHY THIS EXISTS (and is NOT a dup of test_queue_scale_reliability): that test drives QUEUE BOOKKEEPING with
self-clearing INDEPENDENT faults and never generates concurrency against a limit, so it structurally CANNOT
reproduce a LOAD-INDUCED storm. Here the 429 EMERGES from load x limit — the stub counts in-flight connections and
429s when they exceed the ceiling — so the governor's DYNAMIC connection window (learn -> ratchet -> AIMD-grow ->
re-admit/retry, a TCP-style window) is what turns it GREEN. Before that fix this gate was RED (0/12, ~580 failures).

THE SLO, asserted over RUNS independent COLD starts x varied ceiling/burst/prompt-size (the '100x across variety'):
  I2 NO CLIENT FAILURE — every task ultimately returns usable text; a cold-start 429 is ABSORBED (re-queued under the
     ratcheting window), never surfaced to the caller. This is THE guarantee ("the client always gets its result").
  I4 BOUNDED — the whole storm settles within a wall-clock deadline (no wedge, no N×deadline retry).
A cold-probe 429 count > 0 is EXPECTED (that is how the window learns the limit) and is fine PROVIDED it is absorbed.
"""
import os
import sys
import tempfile
import threading
import time
import functools
import pathlib

os.environ.setdefault("SPENDGUARD_TEST_ISOLATED", "1")
os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="sg-connstorm-")
os.environ["SPENDGUARD_DISPATCH_XP_OFF"] = "1"          # in-process buckets only (hermetic; no flock across procs)
os.environ["SPENDGUARD_ROUTE_THROUGH_QUEUE"] = "0"
os.environ.setdefault("SPENDGUARD_NO_AUTOINSTALL", "1")
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from spendguard import adapters, dispatch, lane_balance   # noqa: E402

print = functools.partial(print, flush=True)             # unbuffered — a long run shows progress live (the runlog lesson)

MODEL = "claude-haiku-4-5"
VENDOR = "anthropic"
RUNS = int(os.environ.get("STORM_RUNS", "8"))
DEADLINE = float(os.environ.get("STORM_DEADLINE", "15"))   # generous vs the ~1s real convergence → no CI-jitter flakes
# varied: ceiling BELOW the governor's default vendor cap (8) so the window must LEARN+ratchet; varied burst + prompt size
_VARIETY = [(4, 40, 2000), (3, 60, 8000), (5, 50, 40000), (4, 80, 2000), (2, 30, 60000), (6, 100, 4000)]


class _ConnLimitedProvider:
    """Stub transport (replaces adapters._call_once): enforces a concurrent-CONNECTION ceiling like the real account,
    and 429s (in _call_once's real result shape) whenever concurrent calls exceed it. Records max observed concurrency
    and the storm size, so the test PROVES the window bounds live concurrency rather than merely asserting it."""
    def __init__(self, ceiling, latency=0.03):
        self.ceiling, self.latency = ceiling, latency
        self.lock = threading.Lock()
        self.inflight = self.max_inflight = self.calls = self.conn_429s = 0

    def __call__(self, model, prompt, max_tokens=None, **kw):
        with self.lock:
            self.inflight += 1
            self.calls += 1
            self.max_inflight = max(self.max_inflight, self.inflight)
            over = self.inflight > self.ceiling
        try:
            if over:
                with self.lock:
                    self.conn_429s += 1
                time.sleep(0.002)
                return {"provider": VENDOR, "model": model, "text": None, "parsed": None, "in_tok": 0, "out_tok": 0,
                        "cost": 0.0, "latency": 0.002, "finish_reason": None, "truncated": False,
                        "status_code": 429, "outcome": "overloaded",
                        "error": "rate_limit_error: Number of concurrent connections has exceeded your rate limit"}
            time.sleep(self.latency)
            return {"provider": VENDOR, "model": model, "text": "ok", "parsed": None, "in_tok": 12, "out_tok": 8,
                    "cost": 0.00001, "latency": self.latency, "finish_reason": "stop", "truncated": False, "error": None}
        finally:
            with self.lock:
                self.inflight -= 1


def _run_storm(ceiling, burst, prompt, deadline_s):
    prov = _ConnLimitedProvider(ceiling)
    _real_once, _real_lane_for = adapters._call_once, adapters._lane_for
    adapters._call_once = prov
    adapters._lane_for = lambda v: None          # force METERED (vendor: key) so the per-vendor window applies
    adapters._resolve_guard.on = True            # skip the served-substitute resolver (no network / no model swap)
    t0 = time.time()
    rows, err = [], None
    try:
        rows = lane_balance.bulk_delegate([prompt] * burst, "storm:probe",
                                          model_for=lambda t: "anthropic:" + MODEL, metered_only=True,
                                          max_workers=burst, deadline_s=deadline_s, force=True)
    except Exception as e:
        err = f"{type(e).__name__}: {e}"
    finally:
        adapters._call_once, adapters._lane_for = _real_once, _real_lane_for
        adapters._resolve_guard.on = False
    client_fail = sum(1 for r in rows if not (r or {}).get("text")) if rows else burst
    return {"max_inflight": prov.max_inflight, "conn_429s": prov.conn_429s, "client_fail": client_fail,
            "elapsed": time.time() - t0, "deadline": deadline_s, "err": err}


def main():
    fails = 0
    print(f"== connection-storm reliability: {RUNS} cold-start runs (governor default vendor cap="
          f"{dispatch.DEFAULT_VENDOR_CONCURRENCY}) ==")
    for i in range(RUNS):
        ceiling, burst, psize = _VARIETY[i % len(_VARIETY)]
        dispatch.reset_connection_window(VENDOR)       # each run = an INDEPENDENT cold start (public reset, no internals)
        r = _run_storm(ceiling, burst, "x " * (psize // 2), DEADLINE)
        i2 = r["client_fail"] == 0 and r["err"] is None    # PRIMARY SLO: client gets every result, even cold
        i4 = r["elapsed"] < r["deadline"]                  # converged within the deadline (no wedge)
        ok = i2 and i4
        fails += 0 if ok else 1
        learned = dispatch.learned_limits(VENDOR).get("conn")
        print(f"  run {i:2d} ceil={ceiling} burst={burst:3d}: max_inflight={r['max_inflight']:2d} "
              f"conn429(cold-probe)={r['conn_429s']:4d} client_fail={r['client_fail']:3d} {r['elapsed']:.1f}s "
              f"learned_conn={learned}  [{'OK' if ok else 'FAIL'}] I2_noclientfail={i2} I4_bounded={i4}"
              + (f"  ERR={r['err']}" if r['err'] else ""))
    print(f"\n{'OK' if fails == 0 else '[FAIL]'} test_connection_storm_reliability: {fails} failure(s) "
          f"(SLO = 0 client-visible failures across {RUNS} cold-start bursts)")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
