"""Shipped catalog prices must be sufficient without the machine-local synced layer."""
import json
import os
from pathlib import Path
import sys

from _pytest_fixture_bridge import align_test_startup_home

align_test_startup_home(prefix="sg-catalog-portable-")
os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"
os.environ.setdefault("SPENDGUARD_NO_AUTOINSTALL", "1")
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from spendguard import model_catalog, pricing  # noqa: E402

SYNTHETIC_OVERLAY_MODEL_ID = "catalog-portability-overlay-only-fixture"
SYNTHETIC_OVERLAY_INPUT_PRICE = 1.0


def catalog_portability_failures():
    shipped_document = json.loads(Path(model_catalog.DATA_PATH).read_text(encoding="utf-8"))
    shipped_models = shipped_document["models"]
    failures = []
    for model_id, record in sorted(shipped_models.items()):
        shipped_price = record.get("price") or {}
        if shipped_price.get("in_") is None:
            continue
        try:
            resolved = pricing.price(model_id)
        except Exception as exc:
            failures.append(f"{model_id}: {type(exc).__name__}: {exc}")
            continue
        for price_axis, shipped_value in shipped_price.items():
            if price_axis in {"source", "verified", "valid_from", "valid_to"}:
                continue
            if resolved.get(price_axis) != shipped_value:
                failures.append(f"{model_id}: {price_axis} resolved={resolved.get(price_axis)!r} shipped={shipped_value!r}")
    overlay_records = json.loads(Path(model_catalog._overlay_path()).read_text()).get("models", {}) \
        if Path(model_catalog._overlay_path()).is_file() else {}
    for model_id, record in sorted(overlay_records.items()):
        if (record.get("price") or {}).get("in_") is not None and model_id not in shipped_models:
            failures.append(f"{model_id}: priced only by catalog_synced.json")
    return failures


def test_catalog_portable_rejects_synced_only_priced_model(tmp_path):
    overlay_path = Path(model_catalog._overlay_path())
    overlay_path.write_text(json.dumps({"models": {SYNTHETIC_OVERLAY_MODEL_ID: {
        "id": SYNTHETIC_OVERLAY_MODEL_ID,
        "price": {"in_": SYNTHETIC_OVERLAY_INPUT_PRICE, "out": 0.0},
    }}}))
    failures = catalog_portability_failures()
    assert any(SYNTHETIC_OVERLAY_MODEL_ID in failure for failure in failures)
    assert catalog_portable_main() == 1
    print("fail-proof catalog-portable injected synced-only model: exit 1")
    overlay_path.unlink()
    assert catalog_portability_failures() == []


def catalog_portable_main():
    failures = catalog_portability_failures()
    for failure in failures:
        print(f"FAIL {failure}")
    return int(bool(failures))


if __name__ == "__main__":
    import tempfile
    with tempfile.TemporaryDirectory() as temporary_directory:
        test_catalog_portable_rejects_synced_only_priced_model(Path(temporary_directory))
    raise SystemExit(catalog_portable_main())
