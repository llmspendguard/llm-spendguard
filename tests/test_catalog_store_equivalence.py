#!/usr/bin/env python3
import os
import sys
import tempfile

REPOSITORY_ROOT_TEXT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="sg-catalog-equivalence-")
os.environ.setdefault("SPENDGUARD_TEST_ISOLATED", "1")
os.environ.setdefault("SPENDGUARD_NO_AUTOINSTALL", "1")
sys.path.insert(0, os.path.join(REPOSITORY_ROOT_TEXT, "src"))

import spendguard
spendguard.require()

import importlib.util
import importlib
import json
import pathlib

import spendguard.store.reference_data as _reference_data_package

_WORKTREE_REFERENCE_DATA = os.path.join(
    REPOSITORY_ROOT_TEXT, "src", "spendguard", "store", "reference_data")
if _WORKTREE_REFERENCE_DATA not in _reference_data_package.__path__:
    _reference_data_package.__path__.insert(0, _WORKTREE_REFERENCE_DATA)
importlib.invalidate_caches()
catalog_store = importlib.import_module("spendguard.store.reference_data.catalog_store")

REPOSITORY_ROOT = pathlib.Path(__file__).resolve().parents[1]
REFERENCE_DATA_DIRECTORY = REPOSITORY_ROOT / "src" / "spendguard" / "store" / "reference_data"
CATALOG_JSON_PATH = REFERENCE_DATA_DIRECTORY / "model_catalog.json"
CATALOG_SEED_PATH = REFERENCE_DATA_DIRECTORY / "catalog_seed.sql"
CATALOG_BUILDER_PATH = REPOSITORY_ROOT / "scripts" / "catalog" / "build_catalog_db.py"
PRICE_CORRUPTION_DELTA = 1


def _load_catalog_builder():
    specification = importlib.util.spec_from_file_location("equivalence_catalog_builder", CATALOG_BUILDER_PATH)
    if specification is None or specification.loader is None:
        raise RuntimeError(f"cannot load catalog builder from {CATALOG_BUILDER_PATH}")
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module.build_catalog_database


def _baseline_price_table(records):
    """Return (price_table, omitted_ids). A record with no current `in_` rate is omitted from the price table
    (matching as_price_table), but the omission is NOT silent here: the ids dropped are returned so the caller can
    prove every one of them is a legitimately-unpriced model (declares price_error / billing_exempt_reason /
    retired_alias_of), never a model that vanished without a trace."""
    result = {}
    omitted_ids = []
    for record_id, record in records.items():
        price = catalog_store.price_for_date_from_record(record) or {}
        if price.get("in_") is None:
            omitted_ids.append(record_id)
            continue
        row = {key: price[key] for key in (
            "in_", "out", "cached_in", "batch_in", "batch_out", "batch_cached_in"
        ) if key in price and price[key] is not None}
        if price.get("source"):
            row["_source"] = price["source"]
        result.setdefault(record.get("provider") or "?", {})[record.get("metered_id") or record_id] = row
    return result, omitted_ids


def _assert_equivalent(records, expected_price_table, expected_unit_prices):
    actual = catalog_store.all_records()
    assert actual, "catalog equivalence must never pass on an empty catalog"
    assert set(actual) == set(records)
    for record_id in sorted(records):
        assert actual[record_id] == records[record_id], record_id
        ceiling = (records[record_id].get("output_ceiling") or {}).get("value")
        expected_ceiling = int(ceiling) if ceiling else None
        assert catalog_store.published_ceiling(record_id) == expected_ceiling, record_id
    assert catalog_store.as_price_table() == expected_price_table
    assert catalog_store.unit_price_table() == expected_unit_prices


def _seed_without_one_model(seed_text):
    lines = seed_text.splitlines()
    for index, line in enumerate(lines):
        if line.startswith("INSERT INTO models "):
            return "\n".join((*lines[:index], *lines[index + 1:], ""))
    raise AssertionError("generated seed has no model INSERT to remove")


def _seed_with_corrupt_price(seed_text, records):
    target_id, target = next((item for item in sorted(records.items()) if item[1].get("price")))
    original_price = target["price"]
    corrupt_price = dict(original_price)
    corrupt_price["in_"] += PRICE_CORRUPTION_DELTA
    original_json = json.dumps(original_price, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    corrupt_json = json.dumps(corrupt_price, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    target_marker = "'" + target_id.replace("'", "''") + "'"
    lines = seed_text.splitlines()
    for index, line in enumerate(lines):
        if line.startswith("INSERT INTO models ") and target_marker in line and original_json in line:
            lines[index] = line.replace(original_json, corrupt_json, 1)
            return "\n".join((*lines, ""))
    raise AssertionError(f"generated seed has no priced INSERT for {target_id}")


def main():
    baseline_document = json.loads(CATALOG_JSON_PATH.read_text(encoding="utf-8"))
    baseline_records = baseline_document["models"]
    expected_price_table, omitted_from_price_table = _baseline_price_table(baseline_records)
    # Completeness proof: a model may leave the price table ONLY by declaring why it is unpriced. Any omission with
    # no price_error / billing_exempt_reason / retired_alias_of would be a silent drop — fail RED instead.
    for omitted_id in omitted_from_price_table:
        omitted_record = baseline_records[omitted_id]
        assert (omitted_record.get("price_error") or omitted_record.get("billing_exempt_reason")
                or omitted_record.get("retired_alias_of")), (
            f"{omitted_id} is omitted from the price table but declares no price_error, billing_exempt_reason, "
            f"or retired_alias_of — a silent drop")
    expected_unit_prices = baseline_document.get("unit_prices") or {}
    seed_text = CATALOG_SEED_PATH.read_text(encoding="utf-8")
    build_catalog_database = _load_catalog_builder()

    with tempfile.TemporaryDirectory() as temporary_directory:
        temporary_root = pathlib.Path(temporary_directory)
        catalog_store.set_catalog_home_provider(lambda: temporary_root)

        good_database = temporary_root / "catalog.sqlite"
        build_catalog_database(CATALOG_SEED_PATH, good_database)
        catalog_store.CATALOG_DATABASE_PATH = str(good_database)
        _assert_equivalent(baseline_records, expected_price_table, expected_unit_prices)

        # Mutation proof A: dropping one model INSERT must make the whole-catalog id-set assertion go RED.
        missing_seed = temporary_root / "missing_model_seed.sql"
        missing_seed.write_text(_seed_without_one_model(seed_text), encoding="utf-8")
        missing_database = temporary_root / "missing_model.sqlite"
        build_catalog_database(missing_seed, missing_database)
        catalog_store.CATALOG_DATABASE_PATH = str(missing_database)
        try:
            _assert_equivalent(baseline_records, expected_price_table, expected_unit_prices)
        except AssertionError:
            pass
        else:
            raise AssertionError("mutation proof failed: deleted model INSERT stayed GREEN")

        # Mutation proof B: corrupting one authored input price must make field/price equality go RED.
        corrupt_seed = temporary_root / "corrupt_price_seed.sql"
        corrupt_seed.write_text(_seed_with_corrupt_price(seed_text, baseline_records), encoding="utf-8")
        corrupt_database = temporary_root / "corrupt_price.sqlite"
        build_catalog_database(corrupt_seed, corrupt_database)
        catalog_store.CATALOG_DATABASE_PATH = str(corrupt_database)
        try:
            _assert_equivalent(baseline_records, expected_price_table, expected_unit_prices)
        except AssertionError:
            pass
        else:
            raise AssertionError("mutation proof failed: corrupted price stayed GREEN")

        catalog_store.CATALOG_DATABASE_PATH = str(good_database)
        _assert_equivalent(baseline_records, expected_price_table, expected_unit_prices)

    print(f"PASS catalog equivalence ids={len(baseline_records)} mutation_missing=RED mutation_price=RED restored=GREEN")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
