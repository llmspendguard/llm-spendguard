"""Offline test for the canonical pricing math — the founding-bug class. NO network."""
import os, json, datetime
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

print("-- fine-tuned (ft:) resolution: ft entry or LOUD failure, NEVER the base price --")
check("ft normalize strips org+job, keeps ft + canonical base",
      P.normalize("ft:gpt-4o-mini-2024-07-18:acme::job123") == "ft:gpt-4o-mini")
check("ft normalize bare", P.normalize("ft:gpt-4o-mini") == "ft:gpt-4o-mini")
P.PRICING["ft:gpt-4o-mini"] = {"in_": 0.3, "out": 1.2, "cached_in": 0.15, "batch_in": 0.15, "batch_out": 0.6}
check("ft id resolves to the ft entry", P.price("ft:gpt-4o-mini:acme::j1")["in_"] == 0.3)
del P.PRICING["ft:gpt-4o-mini"]
P.PRICING["ft:gpt-4o-mini-2024-07-18"] = {"in_": 0.3, "out": 1.2, "cached_in": 0.15, "batch_in": 0.15, "batch_out": 0.6}
check("ft id resolves via a DATED variant (LiteLLM layer keys)", P.price("ft:gpt-4o-mini:acme::j1")["out"] == 1.2)
del P.PRICING["ft:gpt-4o-mini-2024-07-18"]
try:
    P.price("ft:gpt-4o-mini:acme::j1")               # base gpt-4o-mini IS priced — must NOT be used
    check("unpriced ft NEVER falls back to the base price (raises)", False)
except KeyError as e:
    check("unpriced ft NEVER falls back to the base price (raises)", "BASE price is NOT a substitute" in str(e))

print("-- cached tokens never INFLATE cost (clamp) --")
no_cache = P.realtime_cost("gpt-5.5", 1000, 0, 0)
over = P.realtime_cost("gpt-5.5", 1000, 0, 2000)          # cached > input → must clamp, not inflate
check("cached>input clamps (not more than no-cache)", over <= no_cache + 1e-12)
check("cached>input == fully-cached", abs(over - P.realtime_cost("gpt-5.5", 1000, 0, 1000)) < 1e-12)
check("partial cache is cheaper than none", P.realtime_cost("gpt-5.5", 1000, 0, 500) < no_cache)

check("pricing.verify() self-check passes", P.verify() is True)

print("-- normalize(None) must error, never silently price a None model --")
try:
    P.normalize(None)
    check("normalize(None) raises", False)
except ValueError:
    check("normalize(None) raises ValueError", True)

print("-- providers(): {provider: [models]} split --")
provs = P.providers()
check("openai + anthropic groups exist", "openai" in provs and "anthropic" in provs)
check("claude-* under anthropic, gpt-* under openai",
      any("claude" in m for m in provs["anthropic"]) and any(m.startswith("gpt") for m in provs["openai"]))

print("-- freshness(): days-old + stale flag vs STALE_AFTER_DAYS --")
_v, _s = P.PRICING_VERIFIED, P.STALE_AFTER_DAYS
try:
    P.PRICING_VERIFIED, P.STALE_AFTER_DAYS = "2026-01-01", 45
    _, days, stale = P.freshness(today=datetime.date(2026, 1, 10))
    check("9 days old → not stale", days == 9 and stale is False)
    _, days2, stale2 = P.freshness(today=datetime.date(2026, 6, 1))
    check("151 days old → stale", days2 == 151 and stale2 is True)
    P.PRICING_VERIFIED = "not-a-date"
    _, days3, stale3 = P.freshness(today=datetime.date(2026, 6, 1))
    check("unparseable verified date → (None, False)", days3 is None and stale3 is False)
finally:
    P.PRICING_VERIFIED, P.STALE_AFTER_DAYS = _v, _s

print("-- _load(): a LiteLLM cache in SPENDGUARD_HOME layers in breadth + providers --")
home = os.environ.get("SPENDGUARD_HOME") or os.path.expanduser("~/.spendguard")
os.makedirs(home, exist_ok=True)
with open(os.path.join(home, "litellm_prices.json"), "w") as f:
    json.dump({"models": {"vendor-x-mega": {"in_": 1.0, "out": 2.0, "cached_in": 0.1, "batch_in": 0.5, "batch_out": 1.0}},
               "providers": {"vendor-x-mega": "vendorx"}}, f)
loaded = P._load()
check("_load layers the LiteLLM cache model in", "vendor-x-mega" in loaded)
check("_load layers the cache provider mapping", P.PROVIDERS.get("vendor-x-mega") == "vendorx")

print("-- main(): prints the table + worked example, returns 0 --")
check("main() == 0", P.main() == 0)

print("done.")
