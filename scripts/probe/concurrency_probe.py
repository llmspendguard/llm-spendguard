"""Isolate whether CONCURRENCY itself delivers on the reliable (metered) path — or is rate-limited/serialized.

Times N identical advisor_model calls SERIAL vs CONCURRENT on the RAW pool: adapters._call_once directly, which
bypasses lane routing AND the dispatch governor (those live in the call()/dispatch layer above). So this measures
the pure provider + client concurrency, nothing else. Measurement only — it makes real metered calls (small), and
prints a zero-spend estimate first.

  .venv.nosync/bin/python scripts/probe/concurrency_probe.py [N] [model]     (default N=16, model=advisor_model)
"""
import sys
import time
import concurrent.futures as cf

import spendguard  # noqa: F401  (gated package)
from spendguard import adapters, config, pricing

N = int(sys.argv[1]) if len(sys.argv) > 1 else 16
MODEL = sys.argv[2] if len(sys.argv) > 2 else config.advisor_model()
PROMPT = "Reply with exactly one word: pong."
MAX_TOK = 16
TIMEOUT = 60.0


def _one():
    t0 = time.perf_counter()
    r = adapters._call_once(MODEL, PROMPT, max_tokens=MAX_TOK, timeout_s=TIMEOUT)
    return {"dt": time.perf_counter() - t0, "err": (r or {}).get("error"), "cost": (r or {}).get("cost") or 0.0}


def _pctl(vals, q):
    s = sorted(vals)
    return s[min(len(s) - 1, int(round(q * (len(s) - 1))))] if s else None


def _summ(tag, results, wall):
    dts = [r["dt"] for r in results]
    errs = sum(1 for r in results if r["err"])
    cost = sum(r["cost"] for r in results)
    print(f"  {tag:<10} wall {wall:6.1f}s | per-call p50 {_pctl(dts, .5):5.1f}s p99 {_pctl(dts, .99):5.1f}s "
          f"| {errs}/{len(results)} errors | ${cost:.4f}")
    return wall, errs, cost


# estimate-first (zero spend): 2*N calls, tiny in/out
est = None
try:
    est = pricing.realtime_cost(MODEL, (len(PROMPT) // 4 + 8), MAX_TOK) * 2 * N
except Exception:
    pass
print(f"[concurrency-probe] model={MODEL}  N={N}  (2N={2 * N} metered calls)  est ≈ "
      f"{('$%.4f' % est) if est is not None else 'n/a'}\n")

print("SERIAL (one at a time — the baseline; wall ≈ Σ latency):")
_t = time.perf_counter()
serial = [_one() for _ in range(N)]
s_wall, s_err, s_cost = _summ("serial", serial, time.perf_counter() - _t)

print("\nCONCURRENT (raw ThreadPool, all N at once — no lanes, no governor):")
_t = time.perf_counter()
with cf.ThreadPoolExecutor(max_workers=N) as ex:
    conc = list(ex.map(lambda _i: _one(), range(N)))
c_wall, c_err, c_cost = _summ("concurrent", conc, time.perf_counter() - _t)

# PURE-SDK isolation (openai only): does the PROVIDER + client parallelize with NO spendguard routing at all?
# If this parallelizes but _call_once above does not, the serialization is spendguard's path (lane-first /
# per-call client), not the provider. One shared client (httpx pools + is thread-safe for concurrent requests).
if MODEL.startswith("openai:") or MODEL.startswith("gpt-"):
    try:
        from openai import OpenAI
        raw = MODEL.split(":", 1)[-1]
        _client = OpenAI(api_key=config.api_key("OPENAI_API_KEY"))

        def _raw_one():
            t0 = time.perf_counter()
            try:
                _client.chat.completions.create(model=raw, messages=[{"role": "user", "content": PROMPT}],
                                                max_completion_tokens=MAX_TOK)
                err = None
            except Exception as e:
                err = str(e)[:60]
            return {"dt": time.perf_counter() - t0, "err": err, "cost": 0.0}

        print("\nPURE OpenAI SDK (one shared client, direct .create — NO spendguard, NO lane, NO gate routing):")
        _t = time.perf_counter()
        rs = [_raw_one() for _ in range(N)]
        _summ("raw-serial", rs, time.perf_counter() - _t)
        _t = time.perf_counter()
        with cf.ThreadPoolExecutor(max_workers=N) as ex:
            rc = list(ex.map(lambda _i: _raw_one(), range(N)))
        rc_wall, _re, _rc = _summ("raw-conc", rc, time.perf_counter() - _t)
        rs_wall = sum(r["dt"] for r in rs)
        print(f"  RAW SPEEDUP  Σlatency/concurrent-wall = {(rs_wall / rc_wall) if rc_wall else 0:.2f}x   "
              f"(this is the PROVIDER ceiling; if >> the _call_once speedup, spendguard's path is the bottleneck)")
        _client.close()
    except Exception as _e:
        print(f"\n  (pure-SDK phase skipped: {type(_e).__name__}: {str(_e)[:80]})")

speedup = (s_wall / c_wall) if c_wall > 0 else 0.0
# Report the measured number, not a thresholded verdict — the READER judges against N and the per-call
# latency spread above. Interpretation guide (a fact about the ratio, not a decision): ≈N = fully parallel;
# ≈1 = fully serialized; in between = parallel up to a saturation point (provider/account limit or a pool cap).
print(f"\n  SPEEDUP  serial/concurrent = {speedup:.2f}x   (ideal ≈ {N}x if fully parallel; ≈1x = serialized)")
print(f"  per-call p50: serial {_pctl([r['dt'] for r in serial], .5):.1f}s → concurrent "
      f"{_pctl([r['dt'] for r in conc], .5):.1f}s   (an 8x jump under load is the lock/rate-limit signature)")
print(f"  total spend this probe: ${s_cost + c_cost:.4f}")
