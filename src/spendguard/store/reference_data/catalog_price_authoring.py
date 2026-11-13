"""Provenance-required catalog mutations through the model catalog gateway."""
import copy
import datetime
import math

from . import model_catalog

CATALOG_AUTHOR_LOCK_SUFFIX = ".price-author.lock"
CATALOG_AUTHOR_BACKUP_SUFFIX = "~"
CATALOG_AUTHOR_STAGE_SUFFIX = ".price-author.tmp"
CATALOG_AUTHOR_ENCODING = "utf-8"
CATALOG_AUTHOR_JSON_INDENT = 2
CATALOG_AUTHOR_MIN_RATE = 0
CATALOG_LOCAL_AUTHOR_IDS_KEY = "_local_price_authors"
CATALOG_PRICE_RATE_FIELDS = ("in_", "out", "cached_in", "batch_in", "batch_out", "batch_cached_in")
CATALOG_SYNC_AUTHOR_REASON = "refresh server catalog records while retaining local authorship"


def _catalog_authoring_rate(value, field):
    """Parse an explicitly supplied nonnegative finite per-million rate."""
    if isinstance(value, bool):
        raise ValueError(f"{field} must be a finite nonnegative rate")
    try:
        rate = float(value)
    except (ValueError, TypeError) as exc:
        raise ValueError(f"{field} must be a finite nonnegative rate") from exc
    if not math.isfinite(rate) or rate < CATALOG_AUTHOR_MIN_RATE:
        raise ValueError(f"{field} must be a finite nonnegative rate")
    return rate


def _catalog_price_updates_for_verified_rate(record, current_price, price, verified):
    """Build the record update for a verified rate without mutating its history."""
    rates_changed = current_price is not None and any(
        current_price.get(field) != price.get(field) for field in CATALOG_PRICE_RATE_FIELDS)
    if rates_changed:
        current_start = current_price.get("valid_from")
        if not current_start or verified <= current_start:
            raise ValueError("a changed price verified date must be after the current interval start")
        intervals = [dict(interval) for interval in record.get("price_intervals", [])]
        if not intervals and record.get("price"):
            legacy = dict(current_price)
            legacy["valid_until"] = verified
            intervals.append(legacy)
        else:
            open_intervals = [interval for interval in intervals if interval.get("valid_until") is None]
            if len(open_intervals) != 1:
                raise ValueError("catalog price history must have exactly one open interval")
            open_intervals[0]["valid_until"] = verified
        intervals.append(price)
        record.pop("price", None)
        return {"price_intervals": intervals, "price_error": None}
    if record.get("price_intervals"):
        intervals = [dict(interval) for interval in record["price_intervals"]]
        open_intervals = [interval for interval in intervals if interval.get("valid_until") is None]
        if len(open_intervals) != 1:
            raise ValueError("catalog price history must have exactly one open interval")
        open_intervals[0].update(price)
        return {"price_intervals": intervals, "price_error": None}
    return {"price": price, "price_error": None}


def set_catalog_model_price(model, *, input, output, cached=None, batch_input=None,
                            batch_output=None, provider=None, source, verified):
    """Write one price to the configured local layer, retaining unspecified fields.

New records require an explicit provider. Missing optional rates remain unknown;
no cache or batch discount is invented. Dates and provenance are mandatory even
for updates. Corrupt/missing catalogs and failed snapshots fail closed.
    """
    if not isinstance(model, str) or not model.strip():
        raise ValueError("model is required")
    if not isinstance(source, str) or not source.strip():
        raise ValueError("source is required (provider pricing URL or invoice)")
    try:
        verified_date = datetime.date.fromisoformat(verified)
    except (TypeError, ValueError) as exc:
        raise ValueError("verified is required as a YYYY-MM-DD date") from exc
    today = datetime.datetime.now(datetime.timezone.utc).date()
    if verified_date.isoformat() != verified or verified_date > today:
        raise ValueError("verified must be a non-future YYYY-MM-DD date")
    supplied = {"in_": input, "out": output, "cached_in": cached,
                "batch_in": batch_input, "batch_out": batch_output}
    rates = {key: _catalog_authoring_rate(value, key)
             for key, value in supplied.items() if value is not None}
    if input is None or output is None:
        raise ValueError("input and output rates are required")
    authored_price_result = {}

    def _apply_verified_catalog_price(document, snapshot):
        records = document.setdefault("models", {})
        if not isinstance(records, dict):
            raise ValueError("catalog models must be an object")
        existing = model_catalog.catalog_record_from_snapshot(model, {**snapshot, **records})
        canonical = existing["id"] if existing else model.strip()
        record = records.get(canonical)
        if record is None and existing is not None:
            record = copy.deepcopy(existing)
            records[canonical] = record
        if record is None:
            if not isinstance(provider, str) or not provider.strip():
                raise ValueError("provider is required for a new catalog model")
            record = {"id": canonical, "provider": provider.strip(), "aliases": []}
            records[canonical] = record
        elif provider is not None and provider != record.get("provider"):
            raise ValueError("provider does not match the authored catalog record")
        current_price = model_catalog.price_for_date_from_record(record)
        price = dict(current_price or {})
        price.update(rates)
        price.update(source=source.strip(), verified=verified)
        price.pop("valid_until", None)
        price["valid_from"] = verified
        updates = _catalog_price_updates_for_verified_rate(record, current_price, price, verified)
        model_catalog.merge_row_updates(record, updates, today=today)
        local_authors = _catalog_local_author_ids(document)
        document[CATALOG_LOCAL_AUTHOR_IDS_KEY] = sorted(local_authors | {canonical})
        authored_price_result.update({**price, "_source": price["source"], "_verified": verified,
                                      "provider": record["provider"]})

    destination = model_catalog.author_catalog_records(_apply_verified_catalog_price, reason=source.strip())
    return destination, authored_price_result


def _catalog_local_author_ids(document):
    """Validate the explicit ownership metadata; malformed ownership cannot permit overwrites."""
    identifiers = document.get(CATALOG_LOCAL_AUTHOR_IDS_KEY, [])
    if not isinstance(identifiers, list) or any(not isinstance(item, str) for item in identifiers):
        raise ValueError("GAP catalog_author_ownership_corrupt: local author IDs must be strings")
    return set(identifiers)


_catalog_update_json_provider = None


def set_catalog_update_json_provider(provider):
    """Retain the legacy rewriter injection contract for compatibility.

Catalog writes use model_catalog.author_catalog_records; this hook remains for
consumers that resolve the historical provider independently.
    """
    global _catalog_update_json_provider
    _catalog_update_json_provider = provider


def _resolve_catalog_update_json():
    """Resolve the compatibility rewriter provider, failing loudly when unset."""
    if _catalog_update_json_provider is None:
        raise RuntimeError(
            "catalog_price_authoring update-json provider is not set; import spendguard (or "
            "spendguard.store.reference_data.pricing) so the catalog rewriter is wired before writing the catalog "
            "layer.")
    return _catalog_update_json_provider


def merge_synced_catalog_layer(models):
    """Refresh server records through the gateway, preserving local authored records."""
    def _apply_synced_catalog_records(document, snapshot):
        records = document.setdefault("models", {})
        if not isinstance(records, dict):
            raise ValueError("GAP catalog_layer_models_corrupt: models must be an object")
        local_authors = _catalog_local_author_ids(document)
        if not local_authors.issubset(records):
            raise ValueError("GAP catalog_author_ownership_corrupt: an owned record is missing")
        records.update({identifier: copy.deepcopy(record) for identifier, record in models.items()
                        if identifier not in local_authors})

    model_catalog.author_catalog_records(_apply_synced_catalog_records, reason=CATALOG_SYNC_AUTHOR_REASON)
    return len(models)
