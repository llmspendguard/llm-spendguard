"""GENERATOR — project the model catalog (the SSOT) into src/spendguard/prices.json (a DERIVED artifact).

prices.json is NOT an authoring point: model prices are authored in model_catalog.json (concern 'model_catalog').
This script regenerates prices.json from the catalog so the two never disagree, and stamps a `_generated` marker so
honestreview's derived_artifact_integrity flags any hand-edit. Run it after changing the catalog (a make/CI target),
never edit prices.json by hand. Idempotent: writes only when the projection actually changed.

Precedence note: pricing._load already layers the catalog directly, so prices.json is primarily a stable, reviewable,
externally-consumable PROJECTION (+ the provenance each rate carries). tests/test_model_catalog_ssot.py fails if it
drifts from the catalog (the freshness guard). $0, no network. Gated: import spendguard; spendguard.require()."""
import datetime
import json
import os
import shutil
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

import spendguard  # noqa: E402
spendguard.require()
from spendguard import model_catalog  # noqa: E402

DEST = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src", "spendguard", "prices.json")


def render():
    """The prices.json content projected from the catalog: {_meta (with the _generated marker), providers:{prov:{models:
    {id: rate-row}}}}. The rate rows and their _source provenance come straight from model_catalog.as_price_table();
    nothing is authored here. Deterministic (sorted) so an unchanged catalog yields byte-identical output."""
    table = model_catalog.as_price_table()
    providers = {}
    for prov in sorted(table):
        providers[prov] = {"models": {mid: table[prov][mid] for mid in sorted(table[prov])}}
    meta = {
        "_generated": True,
        "_generator": "scripts/gen_prices_json.py",
        "_source": "src/spendguard/model_catalog.json (concern 'model_catalog' — the SSOT for model data)",
        "note": "GENERATED from the model catalog — DO NOT EDIT BY HAND. Author prices in model_catalog.json, then run "
                "`python scripts/gen_prices_json.py --write`. honestreview derived_artifact_integrity flags hand-edits.",
        "generated_at": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    return {"_meta": meta, "providers": providers}


def _stable(doc):
    """The doc minus the volatile generated_at timestamp — so 'did the projection change?' compares the DATA, not the
    clock (else every run would rewrite the file and churn a backup)."""
    d = json.loads(json.dumps(doc))
    d.get("_meta", {}).pop("generated_at", None)
    return json.dumps(d, sort_keys=True)


def main(argv=None):
    """`--write` regenerates src/spendguard/prices.json from the catalog (else prints to stdout). On --write: if the
    existing file is NOT yet marked _generated (the one-time migration from the hand-curated file), it is backed up to a
    timestamped .bak first — loss is never silent. If the projection is unchanged (ignoring the timestamp), nothing is
    written. Returns 0."""
    write = "--write" in (argv if argv is not None else sys.argv[1:])
    doc = render()
    text = json.dumps(doc, indent=2, sort_keys=False) + "\n"
    if not write:
        sys.stdout.write(text)
        return 0
    existing = None
    if os.path.exists(DEST):
        try:
            existing = json.load(open(DEST))
        except Exception:
            existing = None
        if existing is not None and _stable(existing) == _stable(doc):
            print(f"unchanged — {DEST} already matches the catalog ({len(doc['providers'])} providers)")
            return 0
        if not (existing or {}).get("_meta", {}).get("_generated"):
            bak = f"{DEST}.{datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.bak"
            shutil.copy2(DEST, bak)
            print(f"migrating hand-curated → generated; backed up existing → {bak}", file=sys.stderr)
    with open(DEST, "w") as f:
        f.write(text)
    n = sum(len(p["models"]) for p in doc["providers"].values())
    print(f"wrote {DEST} — {len(doc['providers'])} providers, {n} models (generated from the catalog)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
