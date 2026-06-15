"""Offline test for reconcile cost-from-usage helpers (no network — pure functions)."""
from spendguard import reconcile_openai as ro, reconcile_anthropic as ra, pricing


def check(name, cond):
    print(f"  [{'OK' if cond else 'FAIL'}] {name}")
    assert cond


print("-- reconcile_openai.day (UTC date from a batch's created_at) --")
check("epoch → UTC date", ro.day({"created_at": 1700000000}) == "2023-11-14")

print("-- reconcile_anthropic._cost (cache-aware, batch rates) --")
u = {"input_tokens": 1000, "cache_read_input_tokens": 500,
     "cache_creation_input_tokens": 200, "output_tokens": 100}
p = pricing.price("claude-opus-4-8")
expect = (1000 * p["batch_in"] + 500 * p["cached_in"] * 0.5
          + 200 * p["batch_in"] * 1.25 + 100 * p["batch_out"]) / 1e6
got = ra._cost("claude-opus-4-8", u)
check(f"cache-aware cost ${got:.6f} == ${expect:.6f}", abs(got - expect) < 1e-12)

print("-- unknown model: record + return 0, never crash or guess --")
ra.UNKNOWN_MODELS.clear()
check("unknown → 0.0", ra._cost("made-up-model", u) == 0.0)
check("unknown recorded for the report's WARN", ra.UNKNOWN_MODELS.get("made-up-model") == 1)
print("done.")
