"""Client data-plane T2 overlay: model_catalog resolution layers a SERVER-SYNCED / local-override catalog
(`catalog_synced.json` in SPENDGUARD_HOME) ABOVE the shipped curated floor (the package `model_catalog.json`).

This is the client half of the data plane's item 2 (docs/DATA_PLANE.md §9.2): a curated fact — e.g. a newly-measured
embed ceiling — can reach a pip user via the overlay WITHOUT a package release, and a user can drop the overlay by
hand to OVERRIDE the floor locally (the resolution order floor→synced→local).

Pins: an overlay record OVERRIDES the floor by model id (embed_batch_ceiling returns the overlay value); a NEW overlay
model appears; an ABSENT overlay = floor only; a CORRUPT overlay degrades to floor-only (never wipes it); and the
cache is CONTENT-keyed — rewriting the overlay is reflected, never served stale (the durability property the memo now
guarantees). Offline, isolated SPENDGUARD_HOME, no network, no spend.
"""
import json
import os
import sys
import tempfile

os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="sg-catalog-overlay-")
os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"
os.environ.setdefault("SPENDGUARD_NO_AUTOINSTALL", "1")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from spendguard import model_catalog   # noqa: E402


def _write_overlay(models):
    with open(model_catalog._overlay_path(), "w") as f:
        json.dump({"models": models}, f)


def _emb(model_id, provider, cap):
    return {model_id: {"id": model_id, "provider": provider,
                       "capabilities": {"mode": "embedding", "embed_max_batch": {"value": cap}}}}


def main():
    fails = []

    def ck(name, cond, extra=""):
        print(("  [OK] " if cond else "  [FAIL] ") + name + (("  — " + str(extra)) if extra and not cond else ""))
        if not cond:
            fails.append(name)

    ovp = model_catalog._overlay_path() or ""
    ck("overlay path is catalog_synced.json under SPENDGUARD_HOME (not the package model_catalog.json)",
       ovp.endswith("catalog_synced.json") and os.environ["SPENDGUARD_HOME"] in ovp, ovp)

    # floor-only baseline (no overlay yet)
    ck("floor-only: gemini-embedding-001 ceiling is the shipped 100",
       model_catalog.embed_batch_ceiling("gemini-embedding-001") == 100,
       model_catalog.embed_batch_ceiling("gemini-embedding-001"))
    floor_n = len(model_catalog._load_records())
    ck("floor-only: the catalog is non-empty", floor_n > 0, floor_n)

    # overlay OVERRIDES a field + ADDS a new model
    _write_overlay({**_emb("gemini-embedding-001", "gemini", 250), **_emb("brand-new-embed-9", "acme", 77)})
    ck("overlay OVERRIDES the floor by id: gemini ceiling now 250 (was 100)",
       model_catalog.embed_batch_ceiling("gemini-embedding-001") == 250,
       model_catalog.embed_batch_ceiling("gemini-embedding-001"))
    ck("overlay ADDS a new model: brand-new-embed-9 → 77", model_catalog.embed_batch_ceiling("brand-new-embed-9") == 77)
    ck("a non-overlaid floor model still resolves (text-embedding-3-small → 2048)",
       model_catalog.embed_batch_ceiling("text-embedding-3-small") == 2048)

    # CONTENT-keyed cache: rewriting the overlay is reflected (no mtime reliance)
    _write_overlay(_emb("gemini-embedding-001", "gemini", 300))
    ck("rewriting the overlay is reflected (content-keyed cache): gemini now 300",
       model_catalog.embed_batch_ceiling("gemini-embedding-001") == 300,
       model_catalog.embed_batch_ceiling("gemini-embedding-001"))

    # CORRUPT overlay → floor only (never wipes the floor)
    with open(model_catalog._overlay_path(), "w") as f:
        f.write("{ this is not json")
    ck("corrupt overlay degrades to floor-only: gemini back to shipped 100",
       model_catalog.embed_batch_ceiling("gemini-embedding-001") == 100)
    ck("corrupt overlay does NOT wipe the floor (catalog still non-empty)",
       len(model_catalog._load_records()) >= floor_n)

    # ABSENT overlay → floor only
    os.remove(model_catalog._overlay_path())
    ck("absent overlay: floor-only restored (gemini 100 + count back to floor)",
       model_catalog.embed_batch_ceiling("gemini-embedding-001") == 100 and len(model_catalog._load_records()) == floor_n)

    print(f"\n{'[FAIL]' if fails else 'OK'} test_catalog_overlay: {len(fails)} failure(s)")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
