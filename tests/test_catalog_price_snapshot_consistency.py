"""Catalog lookups use one fresh record for both rates and provider."""
from unittest.mock import patch

from spendguard import model_catalog, pricing

CATALOG_SNAPSHOT_READS_PER_LOOKUP = 1
CATALOG_SNAPSHOT_GUARD_ID = __name__
RATE_INTERVAL_BOUNDARY_FIELDS = frozenset(("valid_from", "valid_until"))


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


def _assert_pricing_rate_row_excludes_interval_boundaries(record, on_date):
    selected_rate = model_catalog.price_for_date_from_record(record, on_date)
    assert selected_rate is not None
    expected_rate = {
        key: value for key, value in selected_rate.items()
        if key not in RATE_INTERVAL_BOUNDARY_FIELDS
    }
    if expected_rate.get("source") and not expected_rate.get("_source"):
        expected_rate["_source"] = expected_rate.pop("source")
    expected_rate["provider"] = record["provider"]
    with patch.object(model_catalog, "model_record", return_value=record):
        assert pricing._catalog_price(CATALOG_SNAPSHOT_GUARD_ID, as_of=on_date) == expected_rate
        assert pricing.price(CATALOG_SNAPSHOT_GUARD_ID, as_of=on_date) == expected_rate


def test_catalog_rate_rows_exclude_effective_date_boundaries():
    legacy_record = next(
        record for record in model_catalog.all_records().values()
        if (record.get("price") or {}).get("in_") is not None
    )
    on_date = legacy_record["price"].get("verified") or legacy_record["as_of"]
    _assert_pricing_rate_row_excludes_interval_boundaries(legacy_record, on_date)

    interval_record = dict(legacy_record)
    selected_interval = dict(legacy_record["price"])
    selected_interval["valid_from"] = on_date
    selected_interval["valid_until"] = None
    interval_record.pop("price")
    interval_record["price_intervals"] = [selected_interval]
    _assert_pricing_rate_row_excludes_interval_boundaries(interval_record, on_date)


if __name__ == "__main__":
    test_catalog_price_single_fresh_record_snapshot()
    test_catalog_rate_rows_exclude_effective_date_boundaries()
