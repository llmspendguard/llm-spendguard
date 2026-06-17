"""Offline tests for reconcile_anthropic PARSERS — _cost() cache-aware pricing, _h() headers,
and cost_by_day() per-day aggregation from a SEEDED cache. NO network: _get/list_batches/
refresh_cache are never hit (refresh_cache is monkeypatched to a no-op; _key reads env).
"""
import os, sys, json, tempfile
if not os.environ.get("SPENDGUARD_TEST_ISOLATED"):
    os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"
    os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="spendguard-test-")
    os.execv(sys.executable, [sys.executable] + sys.argv)

from spendguard import reconcile_anthropic as ra, pricing

failures = 0
def check(label, cond):
    global failures
    ok = bool(cond)
    if not ok: failures += 1
    print(f"  [{'OK' if ok else 'FAIL'}] {label}")


print("-- _h(k): expected Anthropic headers dict --")
h = ra._h("sk-ant-OFFLINE")
check("x-api-key set", h.get("x-api-key") == "sk-ant-OFFLINE")
check("anthropic-version pinned", h.get("anthropic-version") == "2023-06-01")
check("exactly two headers", set(h) == {"x-api-key", "anthropic-version"})

print("-- _cost(model, u): cache-aware, batch rates, cross-checked vs pricing.py --")
u = {"input_tokens": 1000, "cache_read_input_tokens": 500,
     "cache_creation_input_tokens": 200, "output_tokens": 100}
p = pricing.price("claude-opus-4-8")
expect = (1000 * p["batch_in"] + 500 * p["cached_in"] * 0.5
          + 200 * p["batch_in"] * 1.25 + 100 * p["batch_out"]) / 1e6
got = ra._cost("claude-opus-4-8", u)
check(f"opus-4-8 cost ${got:.6f} == ${expect:.6f}", abs(got - expect) < 1e-12)

# no-cache usage: fresh-in + out only
u2 = {"input_tokens": 2000, "output_tokens": 800}
expect2 = (2000 * p["batch_in"] + 800 * p["batch_out"]) / 1e6
check("no-cache usage cost", abs(ra._cost("claude-opus-4-8", u2) - expect2) < 1e-12)

# different priced model (haiku) routes through its own rates
ph = pricing.price("claude-haiku-4-5")
expect_h = (2000 * ph["batch_in"] + 800 * ph["batch_out"]) / 1e6
check("haiku routes to its own price", abs(ra._cost("claude-haiku-4-5", u2) - expect_h) < 1e-12)

print("-- _cost(): unknown model -> record in UNKNOWN_MODELS, return 0 (never guess/crash) --")
ra.UNKNOWN_MODELS.clear()
check("unknown -> 0.0", ra._cost("made-up-model", u) == 0.0)
check("unknown recorded once", ra.UNKNOWN_MODELS.get("made-up-model") == 1)
ra._cost("made-up-model", u)
check("unknown recorded twice", ra.UNKNOWN_MODELS.get("made-up-model") == 2)

print("-- cost_by_day(): seed the cache file, no-op the network refresh, assert per-day aggregation --")
ra.UNKNOWN_MODELS.clear()
# stub network + key so cost_by_day touches NEITHER provider
ra.refresh_cache = lambda k, cache: 0
ra._key = lambda: "sk-ant-OFFLINE"

# cache record shape written by refresh_cache: {bid: {created_at, cost, by_model:{model:{in,out,cost}}}}
cache = {
    "batch_a": {"created_at": "2026-06-10", "cost": 0.0,
                "by_model": {"claude-opus-4-8": {"in": 1_000_000, "out": 0, "cost": 0.0}}},
    "batch_b": {"created_at": "2026-06-10", "cost": 0.0,
                "by_model": {"claude-opus-4-8": {"in": 0, "out": 1_000_000, "cost": 0.0}}},
    "batch_c": {"created_at": "2026-06-11", "cost": 0.0,
                "by_model": {"claude-haiku-4-5": {"in": 1_000_000, "out": 0, "cost": 0.0}}},
}
with open(ra.CACHE_PATH, "w") as f:
    json.dump(cache, f)

by_day, by_model = ra.cost_by_day()
# opus batch: 1M in = $2.50, 1M out = $12.50 -> 2026-06-10 = $15.00
exp_0610 = pricing.batch_cost("claude-opus-4-8", 1_000_000, 0) + pricing.batch_cost("claude-opus-4-8", 0, 1_000_000)
exp_0611 = pricing.batch_cost("claude-haiku-4-5", 1_000_000, 0)
check(f"2026-06-10 = ${by_day.get('2026-06-10', 0):.4f} == ${exp_0610:.4f}",
      abs(by_day.get("2026-06-10", 0) - exp_0610) < 1e-9)
check(f"2026-06-11 = ${by_day.get('2026-06-11', 0):.4f} == ${exp_0611:.4f}",
      abs(by_day.get("2026-06-11", 0) - exp_0611) < 1e-9)
check("by_model opus aggregated", abs(by_model.get("claude-opus-4-8", 0) - exp_0610) < 1e-9)
check("by_model haiku aggregated", abs(by_model.get("claude-haiku-4-5", 0) - exp_0611) < 1e-9)
check("two days present", set(by_day) == {"2026-06-10", "2026-06-11"})

print("-- cost_by_day(since=...): lower-bound filter drops earlier days --")
by_day2, by_model2 = ra.cost_by_day(since="2026-06-11")
check("since filters out 2026-06-10", "2026-06-10" not in by_day2)
check("since keeps 2026-06-11", abs(by_day2.get("2026-06-11", 0) - exp_0611) < 1e-9)
check("since drops opus from by_model", "claude-opus-4-8" not in by_model2)

print("-- cost_by_day(): unknown model in cache -> $0 + recorded (re-price path) --")
ra.UNKNOWN_MODELS.clear()
cache_unk = {"batch_z": {"created_at": "2026-06-12", "cost": 0.0,
                         "by_model": {"ghost-model": {"in": 1000, "out": 10, "cost": 0.0}}}}
with open(ra.CACHE_PATH, "w") as f:
    json.dump(cache_unk, f)
by_day3, by_model3 = ra.cost_by_day()
check("unknown priced $0", by_day3.get("2026-06-12", -1) == 0.0)
check("unknown recorded by re-price path", ra.UNKNOWN_MODELS.get("ghost-model") == 1)

print("-- cost_by_day(): no cache file -> empty result, no crash --")
if os.path.exists(ra.CACHE_PATH):
    os.remove(ra.CACHE_PATH)
by_day4, by_model4 = ra.cost_by_day()
check("missing cache -> empty by_day", by_day4 == {})
check("missing cache -> empty by_model", by_model4 == {})

print("-- refresh_cache(): NO network — stub _get to canned JSONL, parse + sum per result --")
ra.UNKNOWN_MODELS.clear()
_real_refresh = _orig_list = None  # markers (refresh_cache was replaced above; restore the module's own)
import importlib
ra2 = importlib.reload(ra)         # fresh module so refresh_cache/_get are the real ones again
ra2._key = lambda: "sk-ant-OFFLINE"

# canned batch list (what list_batches would return) — one ended batch with a results_url
_BATCHES = [{"id": "batch_ended", "processing_status": "ended",
             "results_url": "https://example/results", "created_at": "2026-06-14T12:00:00Z"},
            {"id": "batch_running", "processing_status": "in_progress",
             "results_url": None, "created_at": "2026-06-14T12:00:00Z"}]
# canned JSONL results (what _get(results_url).read() would yield) — two messages, opus
_RESULTS = "\n".join(json.dumps({
    "result": {"message": {"model": "claude-opus-4-8",
                           "usage": {"input_tokens": 1000, "output_tokens": 500}}}})
    for _ in range(2)) + "\n\n"   # trailing blank line exercises the skip-empty branch

class _FakeResp:
    def read(self): return _RESULTS.encode()
    def decode(self): return _RESULTS

ra2.list_batches = lambda k: _BATCHES
ra2._get = lambda url, k: _FakeResp()      # only the results_url path uses _get here; no network

cache = {}
new = ra2.refresh_cache("sk-ant-OFFLINE", cache)
check("refresh added exactly the 1 ended batch", new == 1)
check("running/no-results batch skipped", "batch_running" not in cache)
rec = cache.get("batch_ended", {})
check("created_at sliced to YYYY-MM-DD", rec.get("created_at") == "2026-06-14")
bm = rec.get("by_model", {}).get("claude-opus-4-8", {})
check("summed input across 2 results", bm.get("in") == 2000)
check("summed output across 2 results", bm.get("out") == 1000)
exp_cost = 2 * ra2._cost("claude-opus-4-8", {"input_tokens": 1000, "output_tokens": 500})
check("per-result cost summed", abs(rec.get("cost", 0) - exp_cost) < 1e-12)
check("cache file written", os.path.exists(ra2.CACHE_PATH))
# already-cached batch is skipped on a second pass (no re-fetch)
check("second pass adds nothing", ra2.refresh_cache("sk-ant-OFFLINE", cache) == 0)

print("-- main(): smoke run, fully stubbed (no network), exercises print/format branches --")
ra2._key = lambda: "sk-ant-OFFLINE"
ra2.refresh_cache = lambda k, cache: 0    # no network in main's cost_by_day -> refresh
# seed a cache so main has something to format, including an unknown model -> WARN branch
seed = {"b1": {"created_at": "2026-06-10", "cost": 0.0,
               "by_model": {"claude-opus-4-8": {"in": 1_000_000, "out": 0, "cost": 0.0},
                            "ghost": {"in": 10, "out": 1, "cost": 0.0}}}}
with open(ra2.CACHE_PATH, "w") as f:
    json.dump(seed, f)
ra2.UNKNOWN_MODELS.clear()
_argv = sys.argv
try:
    sys.argv = ["prog", "--by-day", "--since", "2026-06-01"]
    ra2.main()
    check("main() ran with --by-day --since", True)
    sys.argv = ["prog"]
    ra2.main()
    check("main() ran with no args", True)
except SystemExit:
    check("main() did not hard-exit", False)
except Exception as e:
    check(f"main() raised: {e}", False)
finally:
    sys.argv = _argv

print(f"\n{'[FAIL]' if failures else 'OK'} test_reconcile_anthropic: {failures} failure(s)")
sys.exit(1 if failures else 0)
