"""Local catalog edits preserve whole records and name corrupt metadata loudly."""
import copy
import datetime
import json
from pathlib import Path
import tempfile
from unittest.mock import patch

from _pytest_fixture_bridge import align_test_startup_home

align_test_startup_home(prefix=__name__)

from spendguard import catalog_price_authoring, config, model_catalog, pricing  # noqa: E402

LAYER_GUARD_FLOOR_FILENAME = "floor.json"
LAYER_GUARD_CORRUPT_BYTES = b'{"providers": {"acme": {"m1": {"in_": 1.0}}}, BROKEN'
LAYER_GUARD_SECTION = "cache_modifiers"
LAYER_GUARD_ENCODING = "utf-8"
LAYER_GUARD_INDENT = 2


def _run_catalog_layer_authoring_integrity():
    candidate = next(record for record in model_catalog.all_records().values()
                     if record.get("price") and record.get("aliases"))
    record = copy.deepcopy(candidate)
    original_price = copy.deepcopy(record["price"])
    verified = datetime.datetime.now(datetime.timezone.utc).date().isoformat()
    with tempfile.TemporaryDirectory() as temporary:
        home = Path(temporary)
        floor = home / LAYER_GUARD_FLOOR_FILENAME
        floor.write_text(json.dumps({"models": {record["id"]: record}}, indent=LAYER_GUARD_INDENT),
                         encoding=LAYER_GUARD_ENCODING)
        original = floor.read_bytes()
        with patch.object(config, "HOME", home), patch.object(model_catalog, "DATA_PATH", str(floor)):
            target = Path(pricing.user_prices_path())
            assert target.parent == home and target != floor
            catalog_price_authoring.set_catalog_model_price(
                record["aliases"][0], input=original_price["in_"], output=original_price["out"],
                source=original_price["source"], verified=verified)
            authored = json.loads(target.read_bytes())["models"][record["id"]]
            for field, value in record.items():
                if field not in {"price", "price_error", "as_of"}:
                    assert authored[field] == value, f"whole record field lost: {field}"
            assert model_catalog.model_record(record["aliases"][0]) == authored
            assert floor.read_bytes() == original
            assert target.read_text(encoding=LAYER_GUARD_ENCODING).count("\n") > LAYER_GUARD_INDENT
            target.write_bytes(LAYER_GUARD_CORRUPT_BYTES)
            try:
                model_catalog.catalog_pricing_metadata(LAYER_GUARD_SECTION)
            except model_catalog.CatalogLayerCorruptionError as exc:
                assert "catalog_layer_corrupt" in str(exc) and str(target) in str(exc)
            else:
                raise AssertionError("corrupt metadata was trusted as complete")
            assert target.read_bytes() == LAYER_GUARD_CORRUPT_BYTES
            assert floor.read_bytes() == original
    print("catalog layer integrity: isolation, full alias record, valid pretty JSON, named corruption PASS")


if __name__ == "__main__":
    _run_catalog_layer_authoring_integrity()
