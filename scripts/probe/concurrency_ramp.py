"""Find a lane/provider's CONCURRENCY CEILING empirically — the max useful in-flight calls before throughput
plateaus or errors climb. Answers "what is this provider's concurrency max" with a measured curve, not a guess.

For each N in a ramp, fire N identical calls at once (adapters._call_once — raw pool, no governor) and record wall
time, throughput (calls/sec), per-call p50/p99, and errors. Throughput RISES with N while the provider parallelises
and FLATTENS at its ceiling; errors climb past it (e.g. codex app-server's -32001 "overloaded"). The knee is the max.

  .venv.nosync/bin/python scripts/probe/concurrency_ramp.py [model] [maxN]     (default model=advisor_model, maxN=16)

Codex note: the WARM DAEMON serialises (one pipe), so to measure the PLAN's ceiling run with
SPENDGUARD_CODEX_DAEMON=0 (cold `codex exec`, a fresh process per call). $0 on plan lanes; metered models bill.
"""
import sys
import time
import concurrent.futures as cf

import spendguard  # noqa: F401
from spendguard import adapters, config

MODEL = sys.argv[1] if len(sys.argv) > 1 else config.advisor_model()
MAXN = int(sys.argv[2]) if len(sys.argv) > 2 else 16
PROMPT = "Reply with exactly one word: pong."
MAX_TOK = 16
TIMEOUT = 90.0
LEVELS = [n for n in (1, 2, 4, 8, 12, 16, 24, 32, 48) if n <= MAXN]


def _one():
    t0 = time.perf_counter()
    r = adapters._call_once(MODEL, PROMPT, max_tokens=MAX_TOK, timeout_s=TIMEOUT)
    return {"dt": time.perf_counter() - t0, "err": (r or {}).get("error")}


def _pctl(vals, q):
    s = sorted(vals)
    return s[min(len(s) - 1, int(round(q * (len(s) - 1))))] if s else 0.0


print(f"[concurrency-ramp] model={MODEL}  levels={LEVELS}  (this tool MEASURES the curve; the ceiling — the knee "
      f"where throughput flattens as p99/errors climb — is a judgement the reader/an LLM makes from it, not a "
      f"number this script decides)\n")
print(f"  {'N':>3} | {'wall':>6} | {'thruput/s':>9} | {'p50':>6} | {'p99':>6} | errors")
print("  " + "-" * 52)
peak_thru, peak_n = 0.0, 0                          # the PEAK MEASURED throughput — a fact, not a ceiling verdict
for n in LEVELS:
    t = time.perf_counter()
    with cf.ThreadPoolExecutor(max_workers=n) as ex:
        rows = list(ex.map(lambda _i: _one(), range(n)))
    wall = time.perf_counter() - t
    dts = [r["dt"] for r in rows]
    errs = sum(1 for r in rows if r["err"])
    thru = (n - errs) / wall if wall > 0 else 0.0
    if thru > peak_thru:                            # track the max throughput OBSERVED (arithmetic on a measured rate)
        peak_thru, peak_n = thru, n
    print(f"  {n:>3} | {wall:6.1f} | {thru:9.2f} | {_pctl(dts, .5):6.1f} | {_pctl(dts, .99):6.1f} | {errs}/{n}")
    if errs >= max(1, n // 2):                      # SAFETY BOUND on the measured error COUNT — stop hammering a
        #                                             provider that is majority-failing. NOT a ceiling verdict: it
        #                                             only means we did not probe higher, so the knee is at or below N.
        print(f"  (stopping the ramp: {errs}/{n} errored — not hammering further; any knee is at or below N={n})")
        break

# Report MEASURED FACTS only. The peak-throughput N is NOT asserted to be "the ceiling" — throughput can plateau
# (9.99 vs 10.00 is noise) while latency/errors sharply worsen, so the usable ceiling is the KNEE, which the reader
# judges from the whole curve above. Deciding it here by argmax would be a mechanical stand-in for that judgement.
print(f"\n  peak MEASURED throughput {peak_thru:.2f} calls/s at N={peak_n}. Read the curve for the KNEE (throughput "
      f"flattening while p99/errors climb) — that is the usable concurrency ceiling, and it is your call, not this "
      f"script's. Set lane_concurrency_<lane> at or below it.")
