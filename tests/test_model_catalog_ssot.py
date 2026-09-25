"""Guard — the model_catalog SSOT (concern 'model_catalog'). ONE authoring home for per-model data; prices.json is a
GENERATED, fresh projection of it; published fields resolve from the version-controlled catalog in a FRESH env (not a
host-local override or the synced breadth cache). This is the check that keeps model data from scattering again.

Proves: (1) the catalog satisfies its DATA CONTRACT (model_catalog.validate_catalog); (2) the models we depend on are present;
(3) prices.json carries the _generated marker AND matches the catalog projection exactly (the freshness guard — a
catalog edit without re-running the generator FAILS here); (4) in an ISOLATED home (no synced cache, no user
prices.json) gemini/deepseek/gpt-5.6/glm price from the catalog with the right ceilings — the durable fix for the
scatter; (5) the retired-alias datum is present; (6) reliability's probe default is catalog-derived, not a stale
literal. Offline: reads the shipped catalog/prices.json; no network, no model call."""
import json
import os
import sys
import tempfile

os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="sg-mcssot-")   # ISOLATED: no synced cache, no user prices.json
os.environ.setdefault("SPENDGUARD_TEST_ISOLATED", "1")
os.environ.setdefault("SPENDGUARD_NO_AUTOINSTALL", "1")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from spendguard import model_catalog as mc  # noqa: E402
from spendguard import pricing  # noqa: E402

_SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src", "spendguard")

_fails = []
def ck(label, cond):
    if not cond:
        _fails.append(label)
    print(f"  [{'OK' if cond else 'FAIL'}] {label}")

# ── (1) the catalog satisfies its DATA CONTRACT ──
print("-- (1) DATA CONTRACT --")
probs = mc.validate_catalog()
ck(f"model_catalog.validate_catalog() is clean ({len(probs)} problems)", probs == [])
ck("the catalog is non-trivial (>= 20 curated models)", len(mc.ids()) >= 20)

# ── (2) the models we depend on are present ──
print("-- (2) coverage of the models we depend on --")
for m in ["claude-opus-4-8", "gpt-5.5", "gpt-5-nano", "gpt-5.6-sol", "gpt-5.6-luna",
          "kimi-k3", "glm-5.3", "deepseek-v4-flash", "gemini-3.8-flash"]:
    ck(f"catalog has a record for {m}", mc.model_record(m) is not None)

# ── (3) prices.json is GENERATED and FRESH (matches the catalog projection) ──
print("-- (3) prices.json is a generated, fresh projection --")
shipped = json.load(open(os.path.join(_SRC, "prices.json")))
ck("prices.json carries the _generated marker", shipped.get("_meta", {}).get("_generated") is True)
ck("prices.json names its generator + source", bool(shipped["_meta"].get("_generator")) and "model_catalog" in shipped["_meta"].get("_source", ""))
shipped_table = {prov: pd.get("models", {}) for prov, pd in (shipped.get("providers") or {}).items()}
ck("prices.json EXACTLY matches the catalog projection (freshness — re-run gen_prices_json after a catalog edit)",
   shipped_table == mc.as_price_table())

# ── (4) FRESH env: published fields resolve from the versioned catalog (the durable fix) ──
print("-- (4) versioned resolution in an isolated env (no synced cache, no user override) --")
for m, lo in [("deepseek:deepseek-v4-flash", 1.0), ("google:gemini-3.8-flash", 1.0),
              ("openai:gpt-5.6-sol", 1.0), ("zai:glm-5.3", 1.0)]:
    try:
        c = pricing.realtime_cost(m, 1_000_000, 1_000_000)
    except Exception as e:
        c = None
        print("      ERR", m, type(e).__name__, str(e).splitlines()[0][:60])
    ck(f"{m} prices from the catalog (=${c})", c is not None and c > lo)
ck("gemini-3.8-flash ceiling resolves from the catalog", pricing.max_output_tokens("gemini-3.8-flash") == 65536)
ck("glm-5.3 ceiling resolves (catalog, not the legacy override)", pricing.max_output_tokens("glm-5.3") == 131072)

# ── (5) the retired-alias datum is authored as data, not a code comment ──
print("-- (5) retired alias --")
ck("deepseek-v4-flash records retired_alias_of=deepseek-flash",
   (mc.model_record("deepseek-v4-flash") or {}).get("retired_alias_of") == "deepseek-flash")

# ── (6) reliability probe default is catalog-derived, not a stale literal ──
print("-- (6) no stale probe literal --")
from spendguard import reliability  # noqa: E402
gem = reliability._probe_default("gemini")
ck("reliability gemini probe default is a real catalog gemini id (not 'gemini-flash-latest')",
   gem is not None and gem != "gemini-flash-latest" and mc.model_record(gem) is not None)

# ── (7) Phase 2: the legacy ceiling tables are GONE and the catalog covers every ceiling they held ──
print("-- (7) ceiling consolidation: legacy tables removed, catalog covers them --")
ck("pricing.MAX_OUT is removed (ceilings live only in the catalog)", not hasattr(pricing, "MAX_OUT"))
ck("pricing._OUTPUT_CEILING_OVERRIDES is removed (ceilings live only in the catalog)",
   not hasattr(pricing, "_OUTPUT_CEILING_OVERRIDES"))
for m, exp in [("claude-opus-4-8", 128000), ("claude-haiku-4-5", 64000), ("claude-sonnet-4-5", 64000),
               ("glm-4.6", 131072), ("glm-4.5-air", 98304), ("glm-5", 131072)]:  # former MAX_OUT + _OVERRIDES values
    ck(f"catalog still yields {m} ceiling {exp} (no regression from removing the legacy tables)",
       pricing.max_output_tokens(m) == exp)

# ── (8) Phase 2: LINEAGE — no price.source is the seed's OpenAI fallback for a non-OpenAI vendor ──
print("-- (8) provenance: published prices carry their real provider origin --")
_misattr = [rid for rid, r in mc.all_records().items()
            if (r.get("price") or {}).get("source") == pricing.PRICING_SOURCE and r.get("provider") != "openai"]
ck("no non-OpenAI model carries the OpenAI seed-fallback source (LINEAGE_TO_ORIGIN)", _misattr == [])

print(f"\n{'[FAIL]' if _fails else 'OK'} test_model_catalog_ssot: {len(_fails)} failure(s)")
sys.exit(1 if _fails else 0)
