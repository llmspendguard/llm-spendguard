"""Catalog data contract and isolated runtime resolution guard.

The catalog is shipped and its price projection is derived only in memory.
"""
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
from functools import partial
from _script_checks import record_script_check
ck = partial(record_script_check, _fails, record_before_print=True)

# ── (1) the catalog satisfies its DATA CONTRACT ──
print("-- (1) DATA CONTRACT --")
probs = mc.validate_catalog()
ck(f"model_catalog.validate_catalog() is clean ({len(probs)} problems)", probs == [])
ck("the catalog is non-trivial (>= 20 curated models)", len(mc.ids()) >= 20)
ck("legacy rows may remain undated during the explicit bootstrap rollout",
   not any("as_of" in p for p in mc.validate_catalog()))
_dated = {"x": {"id": "x", "provider": "p", "as_of": "2026-01-02"}}
ck("strict real YYYY-MM-DD as_of is accepted", mc.validate_catalog(_dated) == [])
ck("malformed as_of is rejected", any("strict YYYY-MM-DD" in p for p in mc.validate_catalog(
   {"x": {"id": "x", "provider": "p", "as_of": "2026-1-2"}})))
ck("impossible as_of is rejected", any("real calendar date" in p for p in mc.validate_catalog(
   {"x": {"id": "x", "provider": "p", "as_of": "2026-02-30"}})))
ck("future as_of is rejected", any("future" in p for p in mc.validate_catalog(
   {"x": {"id": "x", "provider": "p", "as_of": "2999-01-01"}})))
ck("post-bootstrap validation can require every row date", any("missing as_of" in p for p in mc.validate_catalog(
   {"x": {"id": "x", "provider": "p"}}, allow_legacy_missing_as_of=False)))

# ── (2) the models we depend on are present ──
print("-- (2) coverage of the models we depend on --")
for m in ["claude-opus-4-8", "gpt-5.5", "gpt-5-nano", "gpt-5.6-sol", "gpt-5.6-luna",
          "kimi-k3", "glm-5.3", "deepseek-v4-flash", "gemini-3.8-flash"]:
    ck(f"catalog has a record for {m}", mc.model_record(m) is not None)

# ── (3) no second on-disk price source ──
print("-- (3) catalog projection exists only in memory --")
ck("generated token price artifact is absent", not os.path.exists(os.path.join(_SRC, "prices.json")))
ck("pricing has no authored fallback table", not hasattr(pricing, "_FALLBACK"))
for provider, rows in mc.as_price_table().items():
    for model, row in rows.items():
        ck(f"catalog price resolves through runtime API: {provider}/{model}",
           pricing.price(model, provider=provider)["in_"] == row["in_"])

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

# ── (9) Phase 3: scattered per-model literals removed; the shared homes are the sole authors ──
print("-- (9) scattered literals consolidated --")
import spendguard.estimate as _est  # noqa: E402
import spendguard.conv as _conv  # noqa: E402
from spendguard import models as _m, resources as _res  # noqa: E402
ck("estimate has no local MIN_CACHE table (cache minimums are the models SSOT)", not hasattr(_est, "MIN_CACHE"))
ck("conv has no local _RT_MODELS table (family ids owned by resources._family_canonical)", not hasattr(_conv, "_RT_MODELS"))
ck("models.cache_min yields the VERIFIED value, not the old drifted 4096 (opus-4-8 = 1024)",
   _m.cache_min("claude-opus-4-8") == 1024 and _m.cache_min("claude-haiku-4-5") == 2048)
ck("models.cache_min is None for an unknown family (caller stays conservative — no cache assumed)",
   _m.cache_min("no-such-model-xyz") is None)
ck("resources._family_canonical is the ONE family→canonical resolver (maps a family, else None)",
   _res._family_canonical("an opus run") == "claude-opus-4-8" and _res._family_canonical("zzz") is None)
ck("resources._norm_model normalises a SPECIFIC id to itself, not the family default (gpt-4o stays gpt-4o)",
   _res._norm_model("gpt-4o") == "gpt-4o")

print(f"\n{'[FAIL]' if _fails else 'OK'} test_model_catalog_ssot: {len(_fails)} failure(s)")
sys.exit(1 if _fails else 0)
