"""The lane + metered reachability sweep — estimate-first ($0), then a tiny live probe per resource — must (a)
never spend on the estimate, (b) enumerate every configured lane + every KEYED metered provider, (c) build a
reachability matrix, and (d) derive a metered target without a blind hardcode (config → default → cheapest-served).

Offline: adapters.call, the lane probe, config.api_key, and the catalog/pricing are stubbed — no network, no
spend. The load-bearing guarantee is ESTIMATE-FIRST: sweep(run=False) must make ZERO calls.
"""
import os
import sys
import tempfile

os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="sg-reliab-")
os.environ.setdefault("SPENDGUARD_TEST_ISOLATED", "1")
os.environ.setdefault("SPENDGUARD_NO_AUTOINSTALL", "1")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from spendguard import reliability, adapters, config, catalog, pricing, lanes           # noqa: E402

fails = []


def check(name, cond):
    ok = bool(cond)
    print(("  [OK] " if ok else "  [FAIL] ") + name)
    if not ok:
        fails.append(name)


# Two keyed metered providers (openai, deepseek), the rest unkeyed; a live catalog + prices for the derivation.
_KEYED = {"openai", "deepseek"}
config.api_key = lambda name: "k" if any(name.startswith(p.upper()) for p in _KEYED) else None
catalog.live_model_ids = lambda prov: {"openai": ["gpt-5-nano", "babbage-002"], "deepseek": ["deepseek-v4-flash"]}.get(prov)
_PRICES = {"openai:gpt-5-nano": 0.4, "openai:babbage-002": 1.6, "deepseek:deepseek-v4-flash": 0.28}
pricing.price = lambda m: {"out": _PRICES.get(m)}
pricing.realtime_cost = lambda m, i, o: 0.00001

# Count calls so we can PROVE the estimate spends nothing.
_calls = {"n": 0}


def _stub_call(model, prompt, **kw):
    _calls["n"] += 1
    return {"text": "ok", "error": None, "cost": 0.00001, "executor": "api"}


adapters.call = _stub_call
lanes.probe = lambda: [{"lane": "codex", "ok": True, "latency": 2.0}, {"lane": "gemini", "ok": False, "error": "quota"}]


print("-- (d) the metered target is derived, never a blind hardcode --")
config._cfg_get = (lambda orig: (lambda s, k, d=None: {} if (s, k) == ("reliability", "probe_models") else orig(s, k, d)))(config._cfg_get)
check("openai target = the named cheap default (gpt-5-nano, and it IS in the live catalog)", reliability._metered_target("openai") == "gpt-5-nano")
check("deepseek default ('deepseek-chat') absent → derived cheapest-served (deepseek-v4-flash, not babbage)",
      reliability._metered_target("deepseek") == "deepseek-v4-flash")

print("\n-- (b) plan enumerates every lane + every KEYED metered provider --")
pl = reliability.plan()
check("all 4 lanes are in the plan", {l for l, _m in pl["lanes"]} == {"claude-code", "codex", "zai-coding", "gemini"})
check("only the keyed metered providers (openai, deepseek), unkeyed excluded", {p for p, _m in pl["metered"]} == _KEYED)

print("\n-- (a) ESTIMATE-FIRST: sweep(run=False) spends NOTHING --")
_calls["n"] = 0
out = reliability.sweep(run=False)
check("run=False makes ZERO calls (a pure estimate)", _calls["n"] == 0)
check("...and returns a metered $ estimate", out["estimate"]["metered_cost"] > 0 and out["estimate"]["n_metered"] == 2)

print("\n-- (c) sweep(run=True) probes each resource and builds the reachability matrix --")
_calls["n"] = 0
res = reliability.sweep(run=True)
check("one metered call per keyed provider", _calls["n"] == 2)
check("lane matrix from the $0 probe (codex reachable, gemini not)",
      res["lanes"]["codex"]["reachable"] is True and res["lanes"]["gemini"]["reachable"] is False)
check("metered matrix carries model + reachable + cost", res["metered"]["openai"]["reachable"] and res["metered"]["openai"]["model"] == "gpt-5-nano")

print(f"\n{'[FAIL]' if fails else 'OK'} test_reliability: {len(fails)} failure(s)")
sys.exit(1 if fails else 0)
