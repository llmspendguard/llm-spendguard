"""One-shot catalog data migration (Ash 2026-09-25): three consistency/provenance fixes to model_catalog.json.

1. context_window: bare int -> {value, source, verified} — the same provenance shape price/output_ceiling use, so
   every context window RECORDS where it came from ("record where we get what data"). The existing bare ints came
   from the synced LiteLLM cache via the seed (pricing.CONTEXT_LIMITS <- model_prices_and_context_window.json), so
   they are recorded as THAT source with verified=null (NOT provider-checked). The active lane models get
   provider-doc-VERIFIED values (real doc URL + date). null/absent windows stay null (unknown != a fake object).
2. provider consistency: kimi-k2.6 provider 'custom' -> 'moonshot' — there is NO self-hosted kimi (Ash); kimi is
   always Moonshot, k3 or above.
3. provider_base: flag ONE reliable, priced, served BASE model per cloud provider (the tier-3 last-resort fallback
   target — adapters.provider_base_model reads this; config can override). Same-provider by construction, so a
   pinned/consensus call keeps its vendor identity. Self-hosted ('custom') gets NO base (a down GPU box has no
   same-provider cloud guarantee).

Idempotent (re-running is a no-op on already-migrated data). Durability: a .bak snapshot beside the file before the
write, and the file is git-tracked (git is the primary backup — review the diff before committing).

Run under the gate:  ./.venv.nosync/bin/python scripts/migrate/catalog_provenance_and_bases.py
"""
import json
import os
import shutil
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
CATALOG = os.path.abspath(os.path.join(_HERE, "..", "..", "src", "spendguard", "model_catalog.json"))

# the honest origin of the bare ints already in the catalog (seeded from the synced LiteLLM breadth cache)
SYNCED_SRC = "litellm:model_prices_and_context_window.json (synced via seed; NOT provider-verified)"

# provider-doc-VERIFIED input context windows for the active lane models — the authoritative source (web docs).
VERIFIED_CTX = {
    "gemini-3.8-flash": {"value": 1048576, "source": "https://ai.google.dev/gemini-api/docs/models/gemini-3.8-flash",
                         "verified": "2026-09-25"},
    "glm-5.3": {"value": 1000000, "source": "https://docs.z.ai/guides/llm/glm-5.3", "verified": "2026-09-25"},
}

# ONE reliable, priced, served base model per cloud provider (good + always-available as much as possible). These are
# a POLICY choice authored here; override per-provider with advisor.provider_base_model. 'custom' (self-hosted) is
# deliberately omitted — a self-hosted box has no same-provider cloud fallback.
PROVIDER_BASE = {
    "openai": "gpt-5.6-luna",
    "anthropic": "claude-haiku-4-5",
    "gemini": "gemini-3.8-flash",
    "zai": "glm-5.3",
    "deepseek": "deepseek-v4-flash",
    "moonshot": "kimi-k3",
    "qwen": "qwen-plus",
}


def main():
    with open(CATALOG) as f:
        cat = json.load(f)
    models = cat.get("models") or {}

    # (2) kimi provider consistency — no self-hosted kimi
    if "kimi-k2.6" in models and models["kimi-k2.6"].get("provider") != "moonshot":
        models["kimi-k2.6"]["provider"] = "moonshot"
        print("kimi-k2.6 provider: custom -> moonshot")

    # (1) context_window provenance
    ctx_changed = 0
    for mid, rec in models.items():
        if mid in VERIFIED_CTX:
            if rec.get("context_window") != VERIFIED_CTX[mid]:
                rec["context_window"] = dict(VERIFIED_CTX[mid]); ctx_changed += 1
            continue
        cw = rec.get("context_window")
        if isinstance(cw, dict) or cw is None:
            continue                                   # already an object, or unknown (stays null) — leave it
        if isinstance(cw, int):
            rec["context_window"] = {"value": cw, "source": SYNCED_SRC, "verified": None}; ctx_changed += 1
    print(f"context_window -> {{value,source,verified}}: {ctx_changed} record(s)")

    # (3) provider_base flags — exactly one per provider named above; clear any stale flag first (idempotent)
    for rec in models.values():
        rec.pop("provider_base", None)
    base_set = 0
    for prov, base_id in PROVIDER_BASE.items():
        if base_id not in models:
            raise SystemExit(f"REFUSED: provider_base target {base_id!r} for {prov!r} is not a catalog record")
        if models[base_id].get("provider") != prov:
            raise SystemExit(f"REFUSED: {base_id!r} provider is {models[base_id].get('provider')!r}, not {prov!r}")
        if (models[base_id].get("price") or {}).get("in_") is None:
            raise SystemExit(f"REFUSED: base {base_id!r} for {prov!r} is unpriced — a base must be usable")
        models[base_id]["provider_base"] = True
        base_set += 1
    print(f"provider_base flagged: {base_set} provider(s)")

    bak = f"{CATALOG}.bak_{time.strftime('%Y%m%d_%H%M%S')}"
    shutil.copy2(CATALOG, bak)
    with open(CATALOG, "w") as f:
        json.dump(cat, f, indent=2, ensure_ascii=False)
        f.write("\n")
    print(f"wrote {CATALOG}\nbackup {bak}")


if __name__ == "__main__":
    main()
