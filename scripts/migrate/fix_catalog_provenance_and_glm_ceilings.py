"""Phase-2 MIGRATION (one-time, idempotent) on src/spendguard/model_catalog.json:

1. LINEAGE fix — the initial seed fell back to the global PRICING_SOURCE (an OpenAI URL) for any model whose price
   row carried no per-model _source, mis-attributing Anthropic / Gemini / Qwen rates to openai. Re-point each such
   price.source to its REAL provider origin (published fields carry the PROVIDER as origin — DERIVED_COPY_FRESHNESS /
   LINEAGE_TO_ORIGIN). Detection is an EXACT string match against pricing.PRICING_SOURCE (the exact constant the seed
   substituted) — parsing a known artifact, NOT a semantic guess about a URL — then re-sourced by provider.

2. CEILING consolidation — add the output-ceiling-only records for the GLM variants that live in pricing.py's legacy
   _OUTPUT_CEILING_OVERRIDES but not yet in the catalog, so that table (and MAX_OUT) can be REMOVED and the catalog is
   the sole ceiling source with no regression (an absent glm ceiling would floor to 32K). These records carry a
   ceiling + provider, no price (unused legacy models — price stays null, never invented).

Idempotent: only changes rows that still need it. Backs up model_catalog.json (.bak) before writing. $0, no network.
Gated. Run: python scripts/migrate/fix_catalog_provenance_and_glm_ceilings.py --write"""
import argparse
import json
import os
import shutil
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "src"))
import spendguard  # noqa: E402
spendguard.require()
from spendguard import pricing  # noqa: E402  — for the EXACT seed-fallback source constant (PRICING_SOURCE)

DEST = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
                    "src", "spendguard", "model_catalog.json")

# The REAL provider origin for each vendor's published list price (provenance, authored once, by provider).
PROVIDER_SOURCE = {
    "anthropic": "https://claude.com/pricing (Anthropic published list price; re-verified 2026-09-12)",
    "gemini": "https://ai.google.dev/gemini-api/docs/pricing (Google Gemini published pricing)",
    "qwen": "https://www.eesel.ai/blog/qwen-pricing (Alibaba Model Studio list price, 2026-06-14)",
}

# GLM output ceilings from pricing._OUTPUT_CEILING_OVERRIDES (docs.z.ai "Maximum Supported max_tokens"), for the
# variants not yet in the catalog. Ceiling-only (unused legacy models; no price authored — never invented).
GLM_CEILINGS = {"glm-4.5": 98304, "glm-4.5-air": 98304, "glm-4.6": 131072,
                "glm-4.7": 131072, "glm-5": 131072, "glm-5.1": 131072}
GLM_CEILING_SOURCE = "docs.z.ai Maximum Supported max_tokens table (verified 2026-09-25)"


def _blank_reasoning():
    return {"floor": None, "effort_ok": False, "reasoning_floor": None, "reasons_by_default": False,
            "tokens_param": "max_tokens", "style": "none"}


def migrate(doc):
    """Apply both fixes to the loaded catalog dict IN PLACE and return (n_source_fixed, n_ceilings_added). A price row
    is re-sourced ONLY when its source is EXACTLY pricing.PRICING_SOURCE (the seed's substituted fallback) and the
    provider is a non-OpenAI vendor we know the real origin for — an exact-constant match, not a URL guess."""
    models = doc["models"]
    fallback = pricing.PRICING_SOURCE
    fixed = 0
    for rid, rec in models.items():
        prov = rec.get("provider")
        price = rec.get("price") or {}
        if prov in PROVIDER_SOURCE and price.get("source") == fallback:
            price["source"] = PROVIDER_SOURCE[prov]
            fixed += 1
    added = 0
    for mid, ceil in GLM_CEILINGS.items():
        if mid in models:
            continue
        models[mid] = {"id": mid, "provider": "zai", "aliases": [], "retired_alias_of": None, "metered_id": mid,
                       "lane_spellings": {}, "price": None,
                       "price_error": "no curated price (unused legacy GLM variant); ceiling-only record",
                       "output_ceiling": {"value": int(ceil), "source": GLM_CEILING_SOURCE},
                       "context_window": None, "reasoning": _blank_reasoning()}
        added += 1
    doc["models"] = {k: models[k] for k in sorted(models)}   # stable diff
    return fixed, added


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true", help="persist changes to model_catalog.json (backs up first)")
    args = ap.parse_args(argv)
    doc = json.load(open(DEST))
    fixed, added = migrate(doc)
    print(f"provenance fixed: {fixed} model(s) · glm ceiling records added: {added}")
    if not args.write:
        print("(dry run — pass --write to persist)")
        return 0
    if fixed == 0 and added == 0:
        print("nothing to change — already migrated (idempotent)")
        return 0
    bak = f"{DEST}.{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}.bak"
    shutil.copy2(DEST, bak)
    print(f"backed up → {bak}", file=sys.stderr)
    with open(DEST, "w") as f:
        f.write(json.dumps(doc, indent=2, sort_keys=False) + "\n")
    print(f"wrote {DEST} — {len(doc['models'])} models")
    return 0


if __name__ == "__main__":
    sys.exit(main())
