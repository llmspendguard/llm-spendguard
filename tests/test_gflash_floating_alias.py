"""Offline floating Flash alias contract, including malformed-catalog fault injection."""
import copy
import json
from pathlib import Path
from unittest.mock import patch

from spendguard import adapters, model_catalog, pricing

GFLASH_ALIAS = "g-flash"
CURRENT_FLASH_ID = "gemini-3.8-flash"
FUTURE_FLASH_ID = "gemini-3.9-flash"
NUMERIC_FLASH_ID = "gemini-3.10-flash"
EXCLUDED_FLASH_IDS = ("gemini-99-flash-lite", "gemini-99-flash-preview", "gemini-99-flash-high")
MALFORMED_FLASH_ID = "gemini-broken-flash"
TIED_FLASH_ID = "gemini-3.8.0-flash"
CATALOG = json.loads(Path(model_catalog.DATA_PATH).read_text())["models"]


def assert_gflash_snapshot_target(snapshot, expected):
    with patch.object(model_catalog, "_load_records", return_value=snapshot):
        assert model_catalog.model_record(GFLASH_ALIAS) is snapshot[expected]
        assert model_catalog.catalog_record_from_snapshot(GFLASH_ALIAS, snapshot) is snapshot[expected]
        assert model_catalog.model_price(GFLASH_ALIAS) == model_catalog.model_price(expected)
        assert pricing.price(GFLASH_ALIAS) == pricing.price(expected)
        assert adapters.metered_fallback_id(snapshot[expected]["provider"], GFLASH_ALIAS)[0] == expected


def gflash_catalog_with_added_entry(identifier):
    snapshot = copy.deepcopy(CATALOG)
    snapshot[identifier] = copy.deepcopy(snapshot[CURRENT_FLASH_ID])
    snapshot[identifier]["id"] = identifier
    snapshot[identifier]["metered_id"] = identifier
    snapshot[identifier].pop("provider_base", None)
    return snapshot


def assert_gflash_snapshot_refused(snapshot):
    with patch.object(model_catalog, "_load_records", return_value=snapshot):
        for lookup in (model_catalog.model_record, pricing.price):
            try:
                lookup(GFLASH_ALIAS)
            except model_catalog.LatestGeminiFlashResolutionError:
                pass
            else:
                raise AssertionError(f"{lookup.__name__} accepted invalid Flash candidates")


assert_gflash_snapshot_target(CATALOG, CURRENT_FLASH_ID)
assert_gflash_snapshot_target(gflash_catalog_with_added_entry(FUTURE_FLASH_ID), FUTURE_FLASH_ID)
assert_gflash_snapshot_target(gflash_catalog_with_added_entry(NUMERIC_FLASH_ID), NUMERIC_FLASH_ID)
for excluded_identifier in EXCLUDED_FLASH_IDS:
    assert_gflash_snapshot_target(gflash_catalog_with_added_entry(excluded_identifier), CURRENT_FLASH_ID)
assert_gflash_snapshot_refused({})
assert_gflash_snapshot_refused(gflash_catalog_with_added_entry(MALFORMED_FLASH_ID))
assert_gflash_snapshot_refused(gflash_catalog_with_added_entry(TIED_FLASH_ID))
# Even an authored alias or exact row cannot silently pin the floating spelling.
pinned_snapshot = gflash_catalog_with_added_entry(FUTURE_FLASH_ID)
pinned_snapshot[GFLASH_ALIAS] = copy.deepcopy(CATALOG[CURRENT_FLASH_ID])
assert_gflash_snapshot_target(pinned_snapshot, FUTURE_FLASH_ID)
print("OK: current, float, numeric order, exclusions, empty, malformed, ties, alias precedence")
