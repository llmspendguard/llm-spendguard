"""Provenance-required, serialized edits to the authored model catalog.

The lock has a stable inode separate from the atomically replaced catalog. A
verified byte-for-byte snapshot precedes replacement; failure leaves the catalog
untouched and propagates to the caller.
"""
import copy
import datetime
import fcntl
import json
import math
import os
from pathlib import Path
import tempfile

from . import model_catalog

CATALOG_AUTHOR_LOCK_SUFFIX = ".price-author.lock"
CATALOG_AUTHOR_BACKUP_SUFFIX = "~"
CATALOG_AUTHOR_STAGE_SUFFIX = ".price-author.tmp"
CATALOG_AUTHOR_ENCODING = "utf-8"
CATALOG_AUTHOR_JSON_INDENT = 2
CATALOG_AUTHOR_MIN_RATE = 0
CATALOG_LOCAL_AUTHOR_IDS_KEY = "_local_price_authors"


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
    catalog_path = Path(model_catalog.catalog_authoring_path())
    catalog_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = Path(str(catalog_path) + CATALOG_AUTHOR_LOCK_SUFFIX)
    with lock_path.open("a+b") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        original = catalog_path.read_bytes() if catalog_path.exists() else None
        document = model_catalog.catalog_document_from_bytes(original, catalog_path)
        records = document.setdefault("models", {})
        if not isinstance(records, dict):
            raise ValueError("catalog models must be an object")
        # Alias resolution belongs to the catalog read API; resolve inside the
        # lock so another author cannot change it between lookup and update.
        floor = model_catalog.catalog_document_from_bytes(
            Path(model_catalog.DATA_PATH).read_bytes(), model_catalog.DATA_PATH)
        snapshot = {**floor.get("models", {}), **records}
        existing = model_catalog.catalog_record_from_snapshot(model, snapshot)
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
        price = dict(record.get("price") or {})
        price.update(rates)
        price.update(source=source.strip(), verified=verified)
        model_catalog.merge_row_updates(record, {"price": price, "price_error": None}, today=today)
        local_authors = _catalog_local_author_ids(document)
        document[CATALOG_LOCAL_AUTHOR_IDS_KEY] = sorted(local_authors | {canonical})
        encoded = (json.dumps(document, indent=CATALOG_AUTHOR_JSON_INDENT) + "\n").encode(CATALOG_AUTHOR_ENCODING)
        backup_path = Path(str(catalog_path) + CATALOG_AUTHOR_BACKUP_SUFFIX)
        if original is not None:
            backup_path.write_bytes(original)
            if backup_path.read_bytes() != original:
                raise OSError("catalog snapshot verification failed")
        staging_path = None
        try:
            with tempfile.NamedTemporaryFile(dir=catalog_path.parent, prefix=catalog_path.name,
                                             suffix=CATALOG_AUTHOR_STAGE_SUFFIX, delete=False) as staging:
                staging_path = Path(staging.name)
                staging.write(encoded)
                staging.flush()
                os.fsync(staging.fileno())
            if original is not None:
                os.chmod(staging_path, catalog_path.stat().st_mode)
            os.replace(staging_path, catalog_path)
        finally:
            if staging_path is not None:
                staging_path.unlink(missing_ok=True)
    return str(catalog_path), {**price, "_source": price["source"], "_verified": verified,
                               "provider": record["provider"]}


def _catalog_local_author_ids(document):
    """Validate the explicit ownership metadata; malformed ownership cannot permit overwrites."""
    identifiers = document.get(CATALOG_LOCAL_AUTHOR_IDS_KEY, [])
    if not isinstance(identifiers, list) or any(not isinstance(item, str) for item in identifiers):
        raise ValueError("GAP catalog_author_ownership_corrupt: local author IDs must be strings")
    return set(identifiers)


def merge_synced_catalog_layer(models):
    """Refresh server records under the price-author lock, preserving local authored records."""
    from . import config
    catalog_path = Path(model_catalog.catalog_authoring_path())
    catalog_path.parent.mkdir(parents=True, exist_ok=True)
    with Path(str(catalog_path) + CATALOG_AUTHOR_LOCK_SUFFIX).open("a+b") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        original = catalog_path.read_bytes() if catalog_path.exists() else None
        document = model_catalog.catalog_document_from_bytes(original, catalog_path)
        records = document.setdefault("models", {})
        if not isinstance(records, dict):
            raise ValueError("GAP catalog_layer_models_corrupt: models must be an object")
        local_authors = _catalog_local_author_ids(document)
        if not local_authors.issubset(records):
            raise ValueError("GAP catalog_author_ownership_corrupt: an owned record is missing")
        records.update({identifier: copy.deepcopy(record) for identifier, record in models.items()
                        if identifier not in local_authors})
        if original is not None:
            backup = Path(str(catalog_path) + CATALOG_AUTHOR_BACKUP_SUFFIX)
            backup.write_bytes(original)
            if backup.read_bytes() != original:
                raise OSError("catalog snapshot verification failed")
        config.update_json(catalog_path, lambda _previous: document, required=True)
    return len(models)
