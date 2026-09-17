"""A declared lane model is 🟢 only if the lane CLI ACTUALLY serves it — declared+priced+mapped is not enough.

MEASURED 2026-09-16 (warden's pinned describe-bakeoff): `spendguard tiers`/`doctor` reported all 4 cheap lanes 🟢,
but the gemini lane CLI (agy) REJECTS the base id `gemini-3.8-flash` (it serves ONLY tier-suffixed forms), so every
pinned call silently fell back to the METERED API — the 'looks protected, is inert' class one level below
tier_config_report (declaration ✓, serving ✗). Two fixes locked here:

  Fix A — the Gemini lane-boundary composer resolves a BASE id to a served tier suffix (default 'medium'), instead
          of returning the bare id agy rejects.
  Fix B — tier_config.reachability_probe dispatches each declared model through the PRODUCTION path
          (adapters.call, pinned + no_metered_fallback → $0) and reads NOT-served when the lane CLI rejects it,
          so a mapped-but-unaccepted id reads RED (surfaced by `spendguard tiers`/`doctor`) instead of green.

Offline + hermetic: no live CLI, no network, no spend — adapters.call and the lane registry are stubbed.
"""
import os
import sys
import tempfile

if not os.environ.get("SPENDGUARD_TEST_ISOLATED"):
    os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"
    os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="spendguard-reach-")
    os.execv(sys.executable, [sys.executable] + sys.argv)

from spendguard import adapters, tier_config, route_utility, lane_catalog, lanes   # noqa: E402

fails = []


def ck(name, cond):
    print(("  [OK] " if cond else "  [FAIL] ") + name)
    if not cond:
        fails.append(name)


print("-- Fix A: the Gemini composer resolves a base id to a SERVED tier suffix, never the bare id agy rejects --")
ck("a bare base id gets the lane's default tier (medium), not the rejected bare id",
   adapters._compose_gemini_reasoning("gemini-3.8-flash", None) == "gemini-3.8-flash-medium")
ck("an explicit valid tier wins", adapters._compose_gemini_reasoning("gemini-3.8-flash", "high") == "gemini-3.8-flash-high")
ck("an explicit tier REPLACES a stale suffix", adapters._compose_gemini_reasoning("gemini-3.8-flash-low", "high") == "gemini-3.8-flash-high")
ck("an already-suffixed id with no reasoning is kept as-is (servable)",
   adapters._compose_gemini_reasoning("gemini-3.8-flash-low", None) == "gemini-3.8-flash-low")
ck("a reasoning agy cannot spell ('minimal') falls to the default tier, not the bare id",
   adapters._compose_gemini_reasoning("gemini-3.8-flash", "minimal") == "gemini-3.8-flash-medium")

print("-- Fix B: reachability_probe reads a CLI-REJECTED declared model as NOT served (would silently meter) --")
route_utility.tiers = lambda: {"cheap": ["m-good"], "strong": ["m-bad"]}
lanes.lanes_status = lambda: {"executor": "pool", "lanes": [{"lane": "L1", "enabled": True}]}
lane_catalog.lanes = lambda: ["L1"]
lane_catalog.lane_provider = lambda ln: "prov"
lane_catalog.lane_model_for_tier = lambda ln, g: {"cheap": "m-good", "strong": "m-bad"}.get(g)


def _stub_call(pinned, prompt, **kw):
    # a real fungible call would pin + suppress metered fallback; the lane either serves or the CLI rejects it
    if pinned == "prov:m-good":
        return {"text": "OK", "model": "m-good", "cost": 0.0, "executor": "L1"}
    return {"error": "invalid model selection"}          # m-bad: the lane CLI rejects it -> a lane miss, $0 (no meter)


adapters.call = _stub_call
snap = tier_config.reachability_probe(save=True)
by = {r["model"]: r for r in snap["rows"]}
ck("a served declared model reads served=True", by.get("m-good", {}).get("served") is True)
ck("a CLI-REJECTED declared model reads served=False (the RED that stops it hiding behind green)",
   by.get("m-bad", {}).get("served") is False and bool(by.get("m-bad", {}).get("error")))

print("-- the probe persists a snapshot doctor/tiers read without re-probing --")
rows, age = tier_config.cached_reachability()
ck("cached_reachability returns the persisted rows + a non-negative age",
   rows is not None and age is not None and age >= 0 and any(r["model"] == "m-bad" for r in rows))

print(f"\n{'[FAIL]' if fails else 'OK'} test_lane_model_reachability: {len(fails)} failure(s)")
sys.exit(1 if fails else 0)
