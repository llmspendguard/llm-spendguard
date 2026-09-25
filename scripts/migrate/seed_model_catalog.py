"""ONE-TIME MIGRATION SEED — bootstrap src/spendguard/model_catalog.json (the new SSOT for model data) from the
CURRENT, already-correct resolved values, so nothing is retyped or guessed. Run once; after this the catalog is the
authoring home and this script is kept only as the provenance of the initial seed.

For every model spendguard actually uses (the curated prices.json set ∪ the config in-use set: advisor.lane_models,
advisor.tiers, ask.default_vendors, reliability.probe_models, advisor/judge/recall defaults) it resolves each field
from its current owner and records provenance:
  price            ← pricing.price(id, provider)                         (curated prices.json / _FALLBACK / user override)
  output_ceiling   ← pricing.max_output_tokens(id) / catalog live        (MAX_OUT / _OUTPUT_CEILING_OVERRIDES / synced / live)
  context_window   ← pricing.CONTEXT_LIMITS
  reasoning        ← models.profile(id) + models.reasons_by_default(id)  (models._RULES family + facts)
  lane_spellings   ← advisor.lane_models (per lane, suffix-aware)
  aliases/retired  ← the known retirements (deepseek-v4-flash → deepseek-flash) + dated-snapshot handling stays in code

DURABILITY: --write refuses to overwrite an existing model_catalog.json (the authoring home may carry curated edits
this seed cannot reconstruct); --force takes a timestamped .bak first, then overwrites — loss is never silent.
Emits JSON to stdout (review) by default. $0, read-only on the sources. Gated: import spendguard; spendguard.require()."""
import argparse
import json
import os
import shutil
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "src"))

import spendguard  # noqa: E402
spendguard.require()
from spendguard import config, pricing, models, adapters  # noqa: E402

# The one known RETIRED alias with no code mapping today (documented only as a comment in pricing.py). Authored here
# as data. Dated snapshots (claude-haiku-4-5 → …-20251001) stay resolved in code (pricing.normalize / _served_dated_id).
RETIRED = {"deepseek-v4-flash": "deepseek-flash"}

DEST = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
                    "src", "spendguard", "model_catalog.json")


def _split(spec):
    """(provider, base_id) for a 'provider:model[-effortsuffix]' or bare spec. Provider from the ':' prefix (else
    inferred via adapters.provider_for); base via pricing.normalize (strips reasoning suffixes and date snapshots).
    Returns (None, None) for an empty/non-string spec. No exceptions escape."""
    if not isinstance(spec, str) or not spec:
        return None, None
    prov, rest = (spec.split(":", 1) if ":" in spec else (None, spec))
    if prov is None:
        try:
            prov = adapters.provider_for(rest)
        except Exception:
            prov = None
    base = pricing.normalize(rest)
    return prov, base


def _curated_price_models():
    """{base_id: provider} for every model WE authored a price for — pricing._FALLBACK plus the CURATED price JSONs
    (the shipped src/spendguard/prices.json and the user ~/.spendguard/prices.json), NOT the synced LiteLLM breadth
    cache (that 2700-model table is the fallback, never the catalog). Ignores :batch/exacto variant keys (they are
    derived from the base rate, not separate models)."""
    out = {}
    for base, rec in getattr(pricing, "_FALLBACK", {}).items():
        if ":" not in base:
            out.setdefault(base, (rec or {}).get("provider"))
    files = [os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
                          "src", "spendguard", "prices.json"),
             os.path.join(os.environ.get("SPENDGUARD_HOME") or os.path.expanduser("~/.spendguard"), "prices.json")]
    for f in files:
        if not os.path.exists(f):
            continue
        try:
            d = json.load(open(f))
        except Exception:
            continue
        for prov, pd in (d.get("providers") or {}).items():
            for mid in (pd.get("models") or {}):
                if ":" not in mid:
                    out.setdefault(mid, prov)
    return out


def _in_use_models():
    """{base_id: provider} for every model spendguard actually uses — the union of the config in-use set
    (advisor.lane_models / tiers / model / judge_model / recall_model, ask.default_vendors, reliability.probe_models)
    and the CURATED price set (_curated_price_models — NOT the synced breadth). Deduped; first provider seen wins. $0."""
    out = {}

    def add(spec):
        prov, base = _split(spec)
        if base:
            out.setdefault(base, prov)

    lm = config._cfg_get("advisor", "lane_models", {}) or {}
    for v in lm.values():
        for m in (v.values() if isinstance(v, dict) else [v]):
            add(m)
    for tier_models in (config._cfg_get("advisor", "tiers", {}) or {}).values():
        for m in (tier_models or []):
            add(m)
    for m in (config._cfg_get("ask", "default_vendors", "") or "").split(","):
        add(m.strip())
    for m in (config._cfg_get("reliability", "probe_models", {}) or {}).values():
        add(m)
    for key in ("model", "judge_model", "recall_model"):
        add(config._cfg_get("advisor", key, None))
    for base, prov in _curated_price_models().items():
        out.setdefault(base, prov)
    return out


def _price(base, prov):
    """(price_record, error). price_record carries in_/out/cached_in/batch_in/batch_out (+batch_cached_in when the
    provider publishes it) plus source+verified provenance; error is a short string when the model is unpriced (the
    value is then None, never a silent 0). Reads pricing.price (curated > synced > override); no model call."""
    try:
        p = dict(pricing.price(base, prov))
    except Exception as e:
        return None, f"{type(e).__name__}: {str(e).splitlines()[0][:120]}"
    src = p.pop("_source", None) or getattr(pricing, "PRICING_SOURCE", None)
    p.pop("_added", None)
    p.pop("provider", None)
    rec = {"in_": p.get("in_"), "out": p.get("out"), "cached_in": p.get("cached_in"),
           "batch_in": p.get("batch_in"), "batch_out": p.get("batch_out")}
    if "batch_cached_in" in p:
        rec["batch_cached_in"] = p["batch_cached_in"]
    rec["source"] = src
    rec["verified"] = getattr(pricing, "PRICING_VERIFIED", None)
    return rec, None


def _ceiling(base, prov):
    """{value, source} — the model's published max OUTPUT tokens, via pricing.max_output_tokens then the live /models
    catalog; value None (source None) when genuinely unknown (the caller then uses the floor). $0."""
    try:
        v = pricing.max_output_tokens(base)
    except Exception:
        v = None
    src = "pricing.max_output_tokens (MAX_OUT / _OUTPUT_CEILING_OVERRIDES / synced context)"
    if v is None:
        try:
            from spendguard import catalog as _cat
            v = _cat.model_ceiling(prov, base)
            if v:
                src = "live /models catalog"
        except Exception:
            pass
    return {"value": int(v) if v else None, "source": src if v else None}


def _reasoning(base):
    """The model's reasoning contract from models.profile + reasons_by_default: floor (mandatory effort or None),
    effort_ok, reasoning_floor, reasons_by_default, tokens_param, and style ('suffix' for gemini's agy-lane id
    suffix, else 'none' — the lane↔metered equivalence itself stays in code). $0."""
    prof = models.profile(base)
    return {"floor": prof.get("reasoning") if prof.get("reasoning") != "?" else None,
            "effort_ok": bool(prof.get("reasoning_effort_ok")),
            "reasoning_floor": prof.get("reasoning_floor"),
            "reasons_by_default": bool(models.reasons_by_default(base)),
            "tokens_param": prof.get("tokens_param"),
            "style": "suffix" if prof.get("provider") == "gemini" else "none"}


def _lane_spellings(base):
    """{lane: [use-name, …]} — how each subscription lane spells this base model (advisor.lane_models, suffix-aware),
    e.g. {'gemini': ['gemini-3.8-flash-low','gemini-3.8-flash-high']}. {} when no lane runs it. $0."""
    out = {}
    for lane, v in (config._cfg_get("advisor", "lane_models", {}) or {}).items():
        for spelling in (v.values() if isinstance(v, dict) else [v]):
            sp = str(spelling)
            if pricing.normalize(sp.split(":", 1)[-1] if ":" in sp else sp) == base:
                out.setdefault(lane, []).append(sp)
    return {k: sorted(set(v)) for k, v in out.items()}


def build():
    """Resolve every in-use model into one catalog record and return the full {_doc, _seeded_from, models} dict.

    No input. Reads only pricing/models/config/catalog (no network, no model call). Each record:
    {id, provider, aliases, retired_alias_of, metered_id, lane_spellings, price(+source/verified) | price_error,
     output_ceiling{value,source}, context_window, reasoning{…}}. Models are keyed by base id, sorted. A pricing
     failure is recorded in price_error (never a silent 0). Propagates nothing — the field resolvers swallow their
     own source errors into None/error fields."""
    recs = {}
    ctx = getattr(pricing, "CONTEXT_LIMITS", {}) or {}
    for base, prov in sorted(_in_use_models().items()):
        price, price_err = _price(base, prov)
        recs[base] = {"id": base, "provider": prov,
                      "aliases": [], "retired_alias_of": RETIRED.get(base), "metered_id": base,
                      "lane_spellings": _lane_spellings(base),
                      "price": price, "price_error": price_err,
                      "output_ceiling": _ceiling(base, prov),
                      "context_window": (ctx.get(base) or {}).get("max_input_tokens"),
                      "reasoning": _reasoning(base)}
    return {"_doc": "SSOT for per-model data. Authored here (curated edits welcome); prices.json is GENERATED from "
                    "this. See docs/CANONICAL_CONCERNS.json concern 'model_catalog'.",
            "_seeded_from": "scripts/migrate/seed_model_catalog.py (current resolved values)",
            "models": recs}


def main(argv=None):
    """CLI entry. `--write` persists to src/spendguard/model_catalog.json; without it, prints JSON to stdout for
    review. `--write` REFUSES if the file already exists (it may hold curated edits this seed cannot reconstruct);
    `--force` takes a timestamped .bak first, then overwrites — loss is never silent. Returns 0 on success, 2 on a
    refused overwrite. Unpriced models are listed to stderr as a review note (never dropped silently)."""
    ap = argparse.ArgumentParser(description="Seed the model_catalog SSOT from current resolved values.")
    ap.add_argument("--write", action="store_true", help="persist to src/spendguard/model_catalog.json")
    ap.add_argument("--force", action="store_true", help="with --write: back up (.bak) then overwrite an existing file")
    args = ap.parse_args(argv)
    data = build()
    text = json.dumps(data, indent=2, sort_keys=False) + "\n"
    if args.write:
        if os.path.exists(DEST) and not args.force:
            print(f"REFUSED: {DEST} already exists — a re-seed would clobber curated edits it cannot reconstruct. "
                  f"Use --force to back it up (.bak) and overwrite.", file=sys.stderr)
            return 2
        if os.path.exists(DEST):
            bak = f"{DEST}.{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}.bak"
            shutil.copy2(DEST, bak)
            print(f"backed up existing → {bak}", file=sys.stderr)
        with open(DEST, "w") as f:
            f.write(text)
        print(f"wrote {DEST} — {len(data['models'])} models")
    else:
        sys.stdout.write(text)
    unp = [m for m, r in data["models"].items() if not (r.get("price") or {}).get("in_")]
    if unp:
        print(f"\n[seed] {len(unp)} model(s) with no input rate (review): {', '.join(sorted(unp))}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
