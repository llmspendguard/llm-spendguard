"""Offline test for the canonical pricing math — the founding-bug class. NO network."""
from spendguard import pricing as P


def check(name, cond):
    print(f"  [{'OK' if cond else 'FAIL'}] {name}")
    assert cond


print("-- canonical rates (the $149.76 incident was a wrong gpt-5.5 literal) --")
g = P.price("gpt-5.5")
check("gpt-5.5 realtime 5/30", g["in_"] == 5.0 and g["out"] == 30.0)
check("gpt-5.5 batch 2.5/15", g["batch_in"] == 2.5 and g["batch_out"] == 15.0)
o = P.price("claude-opus-4-8")
check("opus-4-8 realtime 5/25", o["in_"] == 5.0 and o["out"] == 25.0)
check("opus-4-8 batch 2.5/12.5", o["batch_in"] == 2.5 and o["batch_out"] == 12.5)

print("-- cost math --")
check("realtime 1M in = $5", abs(P.realtime_cost("gpt-5.5", 1_000_000, 0) - 5.0) < 1e-9)
check("batch 1M in = $2.50 (50% off)", abs(P.batch_cost("gpt-5.5", 1_000_000, 0) - 2.5) < 1e-9)
check("realtime 1M out = $30", abs(P.realtime_cost("gpt-5.5", 0, 1_000_000) - 30.0) < 1e-9)
check("estimate = n × per-item", abs(P.estimate("gpt-5.5", 1000, 100, 50, batch=True)
                                     - P.batch_cost("gpt-5.5", 100000, 50000)) < 1e-12)

print("-- normalize (strip date snapshots) --")
check("OpenAI date", P.normalize("gpt-5.5-2026-04-23") == "gpt-5.5")
check("Anthropic date", P.normalize("claude-haiku-4-5-20251001") == "claude-haiku-4-5")

print("-- unknown model never guesses --")
try:
    P.price("totally-made-up-model")
    check("raises KeyError on unknown", False)
except KeyError:
    check("raises KeyError on unknown", True)

print("-- cached tokens never INFLATE cost (clamp) --")
no_cache = P.realtime_cost("gpt-5.5", 1000, 0, 0)
over = P.realtime_cost("gpt-5.5", 1000, 0, 2000)          # cached > input → must clamp, not inflate
check("cached>input clamps (not more than no-cache)", over <= no_cache + 1e-12)
check("cached>input == fully-cached", abs(over - P.realtime_cost("gpt-5.5", 1000, 0, 1000)) < 1e-12)
check("partial cache is cheaper than none", P.realtime_cost("gpt-5.5", 1000, 0, 500) < no_cache)

check("pricing.verify() self-check passes", P.verify() is True)
print("done.")
