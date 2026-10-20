"""Catalog lookups use one fresh record for both rates and provider."""
from unittest.mock import patch

from spendguard import model_catalog, pricing

CATALOG_SNAPSHOT_READS_PER_LOOKUP = 1
CATALOG_SNAPSHOT_GUARD_ID = __name__


def test_catalog_price_single_fresh_record_snapshot():
    catalog_records = model_catalog.all_records()
    usable_records = tuple(
        record for record in catalog_records.values()
        if (record.get("price") or {}).get("in_") is not None
    )
    assert usable_records
    with patch.object(model_catalog, "model_record") as snapshot_reader:
        for record in usable_records:
            snapshot_reader.return_value = record
            snapshot_reader.reset_mock()
            resolved = pricing._catalog_price(CATALOG_SNAPSHOT_GUARD_ID)
            assert snapshot_reader.call_count == CATALOG_SNAPSHOT_READS_PER_LOOKUP
            expected = dict(record["price"])
            if expected.get("source") and not expected.get("_source"):
                expected["_source"] = expected.pop("source")
            expected["provider"] = record["provider"]
            assert resolved == expected


if __name__ == "__main__":
    test_catalog_price_single_fresh_record_snapshot()
