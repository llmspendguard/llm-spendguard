"""Provenance-required, serialized edits to the authored model catalog.

The lock has a stable inode separate from the atomically replaced catalog. A
verified byte-for-byte snapshot precedes replacement; failure leaves the catalog
untouched and propagates to the caller.
"""
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
    """Write one price to DATA_PATH, retaining unspecified existing price fields.

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
    catalog_path = Path(model_catalog.DATA_PATH)
    lock_path = Path(str(catalog_path) + CATALOG_AUTHOR_LOCK_SUFFIX)
    with lock_path.open("a+b") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        original = catalog_path.read_bytes()
        document = json.loads(original)
        records = document["models"]
        if not isinstance(records, dict):
            raise ValueError("catalog models must be an object")
        # Alias resolution belongs to the catalog read API; resolve inside the
        # lock so another author cannot change it between lookup and update.
        existing = model_catalog.model_record(model)
        canonical = existing["id"] if existing else model.strip()
        record = records.get(canonical)
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
        encoded = (json.dumps(document, indent=CATALOG_AUTHOR_JSON_INDENT) + "\n").encode(CATALOG_AUTHOR_ENCODING)
        backup_path = Path(str(catalog_path) + CATALOG_AUTHOR_BACKUP_SUFFIX)
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
            os.chmod(staging_path, catalog_path.stat().st_mode)
            os.replace(staging_path, catalog_path)
        finally:
            if staging_path is not None:
                staging_path.unlink(missing_ok=True)
    return str(catalog_path), {**price, "_source": price["source"], "_verified": verified,
                               "provider": record["provider"]}
