"""The sole public port for model records, pricing metadata and catalog authoring.

Use author_catalog_records to change a seed or local layer. The private physical
section owns JSON seeds, deterministic SQL generation, SQLite and transactions.

WHY THIS EXISTS: model facts were spread across six owner modules (pricing, models, catalog, lane_registry,
lane_catalog, reasoning_equivalence) + config + scattered literals, with no single per-model record — so a fresh
env had gemini/deepseek unpriced (they lived only in the synced LiteLLM cache), and a model id / price / ceiling /
reasoning-floor could be authored in several places at once. This module is the ONE place a model fact is authored.

Price tables are derived IN MEMORY from this catalog; the synced LiteLLM cache is a BREADTH fallback
for models not in the catalog. PUBLISHED fields (price, output_ceiling) carry the PROVIDER as origin via a `source` +
`verified` provenance; INTERNAL decisions (lane<->metered spelling, chosen reasoning floor) are authored here.

Record schema (the DATA CONTRACT — see validate()):
  id: str (stable key)                      provider: str
  aliases: [str]                            retired_alias_of: str | null
  metered_id: str                           lane_spellings: {lane: [use-name, ...]}
  price: {in_, out, cached_in, batch_in, batch_out, batch_cached_in?, source, verified} | null   price_error: str | null
  output_ceiling: {value: int|null, source: str|null}     context_window: {value: int|null, source, verified} | null
  as_of: "YYYY-MM-DD" | null (last tracked price/capability change; nullable legacy during bootstrap rollout)
  provider_base: true (ONLY on the one reliable base model per provider — the tier-3 fallback target; absent else)
  reasoning: {floor: str|null, effort_ok: bool, reasoning_floor: str|null, reasons_by_default: bool,
              tokens_param: str, style: one of REASONING_STYLES}

No dependency on pricing/models (avoids a circular import — pricing reads THIS at load). $0."""
import copy
import base64
import contextlib
import contextvars
import datetime
import fcntl
import functools
import json
import os
from pathlib import Path
import re
import sqlite3
import tempfile

from .catalog_provenance import PROVENANCE_AUTHORED, PROVENANCE_SOURCED
from .catalog_seed_rendering import render_catalog_seed

_HERE = os.path.dirname(os.path.abspath(__file__))
_CATALOG_JSON_FILENAME = "model_catalog.json"
_CATALOG_DATABASE_PATH = os.path.join(_HERE, "catalog.sqlite")
_CATALOG_SEED_FILENAME = "catalog_seed.sql"
_CATALOG_SCHEMA_FILENAME = "catalog_schema.sql"
_CATALOG_TRANSACTION_SUFFIX = ".catalog-transaction.json"
_CATALOG_LOCAL_SEED_SUFFIX = ".seed.sql"
_CATALOG_BACKUP_SUFFIX = "~"
_CATALOG_STAGE_SUFFIX = ".price-author.tmp"
_CATALOG_ENCODING = "utf-8"
_CATALOG_BASE64_ENCODING = "ascii"
_CATALOG_MEMORY_DATABASE = ":memory:"
_CATALOG_JSON_INDENT = 2
_CATALOG_LAYER_SEED = "seed"
_CATALOG_LAYER_LOCAL = "local"
_CATALOG_WORKSPACE = contextvars.ContextVar("catalog_authoring_workspace", default=None)
_CATALOG_SOURCE_OVERRIDE = contextvars.ContextVar("catalog_source_override", default=None)

MODEL_CATALOG_COMPLETENESS_CONTRACT = {
    "aliases": ("list", "required"),
    "as_of": ("date", "required"),
    "billing_exempt_reason": ("str", "optional"),
    "capabilities": ("dict", "required"),
    "capabilities.embed_max_batch": ("dict", "optional"),
    "capabilities.embed_max_batch.source": ("str", "required"),
    "capabilities.embed_max_batch.value": ("positive_int", "required"),
    "capabilities.function_calling": ("bool", "optional"),
    "capabilities.mode": ("str", "required"),
    "capabilities.parallel_function_calling": ("bool", "optional"),
    "capabilities.pdf_input": ("bool", "optional"),
    "capabilities.prompt_caching": ("bool", "optional"),
    "capabilities.response_schema": ("bool", "optional"),
    "capabilities.tool_choice": ("bool", "optional"),
    "context_window": ("dict", "required"),
    "context_window.source": ("str", "required"),
    "context_window.value": ("positive_int", "required"),
    "context_window.verified": ("date", "required"),
    "id": ("str", "required"),
    "lane_spellings": ("dict", "empty_allowed"),
    "metered_id": ("str", "required"),
    "output_ceiling": ("dict", "required"),
    "output_ceiling.source": ("str", "required"),
    "output_ceiling.value": ("positive_int", "required"),
    "price": ("dict", "required"),
    "price.batch_cached_in": ("number", "optional"),
    "price.batch_in": ("number", "optional"),
    "price.batch_out": ("number", "optional"),
    "price.cached_in": ("number", "optional"),
    "price.in_": ("number", "required"),
    "price.out": ("number", "required"),
    "price.source": ("str", "required"),
    "price.verified": ("date", "required"),
    "price_error": ("str", "nullable"),
    "provider": ("str", "required"),
    "provider_base": ("bool", "optional"),
    "reasoning": ("dict", "required"),
    "reasoning.effort_ok": ("bool", "required"),
    "reasoning.floor": ("str", "nullable"),
    "reasoning.reasoning_floor": ("str", "nullable"),
    "reasoning.reasons_by_default": ("bool", "required"),
    "reasoning.style": ("str", "required"),
    "reasoning.tokens_param": ("str", "required"),
    "retired_alias_of": ("str", "nullable"),
    "vision": ("bool", "required"),
}

REASONING_STYLES = ("param", "suffix", "thinking", "none")   # enum domain for reasoning.style (DATA_CONTRACT)
_REQUIRED_TOP = ("id", "provider")                            # a record must at least identify itself + its vendor
_TRACKED_FIELDS = ("price", "vision", "capabilities", "context_window", "output_ceiling")
_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
BILLING_EXEMPT_REASON_FIELD = "billing_exempt_reason"
CATALOG_UNIT_PRICES_FIELD = "unit_prices"
CATALOG_OVERLAY_FILENAME = "catalog_synced.json"
CATALOG_LOCAL_DATABASE_FILENAME = "catalog_local.sqlite"
GEMINI_FLASH_FLOATING_ALIAS = "g-flash"
GEMINI_FLASH_CATALOG_PROVIDER = "gemini"
GEMINI_FLASH_BASE_SHAPE = re.compile(r"gemini-(?P<version>.*)-flash")
GEMINI_FLASH_NUMERIC_VERSION = re.compile(r"[0-9]+(?:\.[0-9]+)*")
GEMINI_FLASH_VERSION_SEPARATOR = "."
GEMINI_FLASH_VERSION_ZERO = 0


class LatestGeminiFlashResolutionError(ValueError):
    """The complete catalog cannot identify one latest Gemini Flash base."""


def latest_gemini_flash_base_id(models):
    """Return the numeric maximum of exact Gemini Flash base ids in a snapshot.

    ``provider_base`` selects one provider fallback, rather than family membership.
    Exact id grammar excludes lite, preview, and lane effort suffixes.
    """
    version_ids = {}
    for record_id, record in models.items():
        if record.get("provider") != GEMINI_FLASH_CATALOG_PROVIDER:
            continue
        family_match = GEMINI_FLASH_BASE_SHAPE.fullmatch(record_id)
        if family_match is None:
            continue
        version_text = family_match.group("version")
        if GEMINI_FLASH_NUMERIC_VERSION.fullmatch(version_text) is None:
            raise LatestGeminiFlashResolutionError(
                f"GAP gemini_flash_version_unparseable: {record_id!r}")
        version_components = [int(part) for part in version_text.split(GEMINI_FLASH_VERSION_SEPARATOR)]
        while version_components and version_components[-1] == GEMINI_FLASH_VERSION_ZERO:
            version_components.pop()
        version_key = tuple(version_components)
        if version_key in version_ids:
            raise LatestGeminiFlashResolutionError(
                f"GAP gemini_flash_version_tie: {version_ids[version_key]!r}, {record_id!r}")
        version_ids[version_key] = record_id
    if not version_ids:
        raise LatestGeminiFlashResolutionError("GAP gemini_flash_base_absent: no Gemini Flash base model")
    return version_ids[max(version_ids)]


_catalog_home_provider = None


def set_catalog_home_provider(provider):
    """Inject the resolver for the spendguard HOME directory — a zero-arg callable returning the home path.

    model_catalog is a dependency-free leaf by design (see module docstring): it authors per-model data with NO
    import of config/pricing. The two helpers that need the configurable HOME dir (catalog_authoring_path,
    _litellm_breadth_cache) therefore receive it through this provider, wired once by a same-context consumer
    (pricing sets `lambda: config.HOME` at import) instead of model_catalog importing config — which would pull
    this leaf back into the configuration import cycle. Idempotent; last writer wins."""
    global _catalog_home_provider
    _catalog_home_provider = provider


def _resolve_catalog_home():
    """The spendguard HOME directory via the injected provider. Fail LOUD if unset — a missing wiring is a bug to
    surface, never a silently-wrong path (spendguard fail-loud doctrine). Import spendguard (which loads pricing,
    the wirer) before resolving catalog paths."""
    if _catalog_home_provider is None:
        raise RuntimeError(
            "model_catalog catalog-home provider is not set; import spendguard (or "
            "spendguard.store.reference_data.pricing) so the home provider is wired before resolving catalog paths.")
    return _catalog_home_provider()


def catalog_authoring_path():
    """The configurable local catalog layer; the packaged floor is read-only."""
    return os.path.join(str(_resolve_catalog_home()), CATALOG_OVERLAY_FILENAME)


def tracked_catalog_payload(record):
    """Return a stable snapshot of the five fields that own row freshness."""
    record = record or {}
    return {field: copy.deepcopy(record[field]) for field in _TRACKED_FIELDS if field in record}


def merge_row_updates(record, updates, today=None):
    """Merge top-level updates, advancing ``as_of`` only when the tracked payload changed."""
    before = tracked_catalog_payload(record)
    record.update(updates)
    if tracked_catalog_payload(record) != before:
        stamp = today or datetime.datetime.now(datetime.timezone.utc).date()
        if isinstance(stamp, datetime.datetime):
            stamp = stamp.astimezone(datetime.timezone.utc).date()
        stamp = stamp.isoformat() if isinstance(stamp, datetime.date) else str(stamp)
        if not _ISO_DATE.fullmatch(stamp):
            raise ValueError("today must be a YYYY-MM-DD date")
        record["as_of"] = stamp
    return record


def _overlay_path():
    """The SERVER-SYNCED / local-override curated catalog overlay — a {"models": {id: record}} file in SPENDGUARD_HOME,
    the SAME logical shape as the shipped floor. Written through the catalog authoring API from the server's curated
    catalog (the data-plane T2 layer); a user may also drop it by hand to OVERRIDE the shipped floor locally. Distinct
    filename from catalog.py's live served-list cache (HOME/served_model_ids.json), a different store. Configuration
    errors propagate; config is imported lazily, as elsewhere in this leaf, to avoid a load-time cycle."""
    return catalog_authoring_path()


def _local_database_path():
    """The SQLite local catalog layer; local rows win over shipped rows by model id."""
    return os.path.join(str(_resolve_catalog_home()), CATALOG_LOCAL_DATABASE_FILENAME)


def _read_bytes(path):
    """Return file bytes, treating only an absent layer as missing."""
    if not path:
        return None
    try:
        with open(path, "rb") as f:
            return f.read()
    except FileNotFoundError:
        return None


def _parse_models(raw):
    """Return the complete authored model map, refusing a corrupt present layer."""
    document = _catalog_document_or_empty(raw)
    records = document.get("models", {})
    if not isinstance(records, dict):
        raise CatalogLayerCorruptionError("GAP catalog_layer_corrupt: models must be an object")
    return records


def _catalog_document_from_sqlite_bytes(raw):
    connection = sqlite3.connect(_CATALOG_MEMORY_DATABASE)
    try:
        connection.deserialize(raw)
        available = {row[1] for row in connection.execute("PRAGMA table_info(models)")}
        columns = tuple(field for field in _CATALOG_STORAGE_FIELDS if field in available)
        select_columns = ", ".join((*columns, "present_fields"))
        models = {}
        for row in connection.execute(f"SELECT {select_columns} FROM models ORDER BY id"):
            values = dict(zip((*columns, "present_fields"), row))
            present = set(json.loads(values.pop("present_fields")))
            record = {}
            for field in columns:
                if field not in present:
                    continue
                value = values[field]
                if field in _CATALOG_JSON_FIELDS and value is not None:
                    value = json.loads(value)
                elif field in _CATALOG_BOOLEAN_FIELDS and value is not None:
                    value = bool(value)
                record[field] = value
            record_id = record["id"]
            aliases = [item[0] for item in connection.execute(
                "SELECT alias FROM model_aliases WHERE model_id = ? ORDER BY ordinal", (record_id,))]
            if "aliases" in present:
                record["aliases"] = aliases
            lane_spellings = {}
            for lane, spelling in connection.execute(
                    "SELECT lane, spelling FROM model_lane_spellings WHERE model_id = ? ORDER BY lane, ordinal",
                    (record_id,)):
                lane_spellings.setdefault(lane, []).append(spelling)
            if "lane_spellings" in present:
                record["lane_spellings"] = lane_spellings
            models[record_id] = record
        document = {"models": models}
        for section, entry_key, value in connection.execute(
                "SELECT section, entry_key, value FROM catalog_sections ORDER BY section, entry_key"):
            document.setdefault(section, {})[entry_key] = json.loads(value)
        if connection.execute("SELECT name FROM sqlite_master WHERE name = ?", ("catalog_section_shapes",)).fetchone():
            for section, is_object in connection.execute("SELECT section, is_object FROM catalog_section_shapes"):
                if is_object:
                    document.setdefault(section, {})
                else:
                    document[section] = document[section]["value"]
        return document
    finally:
        connection.close()


_CATALOG_STORAGE_FIELDS = (
    "id", "provider", "retired_alias_of", "metered_id", "price", "price_error",
    "output_ceiling", "context_window", "as_of", "provider_base", "reasoning", "vision",
    "capabilities", "billing_exempt_reason",
    "price_intervals",
)
_CATALOG_JSON_FIELDS = frozenset(("price", "output_ceiling", "context_window", "reasoning", "capabilities", "price_intervals"))
_CATALOG_BOOLEAN_FIELDS = frozenset(("provider_base", "vision"))


@functools.lru_cache(maxsize=8)
def _merged_records(floor_b, legacy_overlay_b, local_b):
    """Parse + LAYER the two catalog byte-blobs: the shipped curated floor (T1) OVERLAID by the synced/override layer
    (T2), synced records winning by model id. Keyed by the raw BYTES, so the parse+merge is memoised per exact CONTENT
    — a restore / rsync / iCloud sync / touch that installs DIFFERENT content (even same-size, even with a reset
    mtime) still re-parses, because the bytes (the cache key) differ. There is no mtime/size reliance to go stale.
    ≤8 blobs held; the hot accessor path re-parses only on a genuine content change."""
    return {**_parse_models(floor_b), **_parse_models(legacy_overlay_b), **_parse_models(local_b)}


@functools.lru_cache(maxsize=1)
def _floor_bytes_from_packaged_seed():
    """Serialized SQLite bytes for the BASE catalog, built in memory from the packaged, reviewable catalog_seed.sql.

    The physical catalog.sqlite is an OPTIONAL build artifact (emitted by the wheel's build_py and by dev/test
    builds); an editable or CI install, or any import entry point outside the test harness (e.g. `spendguard pricing`),
    can run before it exists. The seed itself ALWAYS ships with the package, so the base is derived from it here — in
    memory, never writing on import, so a read-only install is safe. Memoised once per process (the seed is immutable
    in an install). Returns None only when the seed is absent. Unreadable or corrupt
    layers fail closed rather than exposing an incomplete catalog."""
    seed = _read_bytes(_catalog_bundle_paths(_CATALOG_LAYER_SEED)[1])
    if seed is None:
        return None
    connection = sqlite3.connect(_CATALOG_MEMORY_DATABASE)
    try:
        connection.executescript(seed.decode("utf-8"))
        return connection.serialize()
    finally:
        connection.close()


def _floor_bytes():
    """The shipped BASE catalog bytes (T1 floor): the physical catalog.sqlite when present, else built from the
    packaged seed. All accessors use this one choke point; tests can override a
    logical document with catalog_source_override, without exposing a JSON path."""
    override = _CATALOG_SOURCE_OVERRIDE.get()
    if override is not None:
        return json.dumps(override).encode(_CATALOG_ENCODING)
    floor_path = _catalog_bundle_paths(_CATALOG_LAYER_SEED)[2]
    floor_b = _read_bytes(floor_path)
    if floor_b is None:
        floor_b = _floor_bytes_from_packaged_seed()
    return floor_b


def _load_records():
    """The {id: record} map, LAYERED per the data plane: the shipped curated floor (T1) OVERLAID by the server-synced
    / local-override catalog (SPENDGUARD_HOME/catalog_synced.json, T2) where present — synced records override/add by
    model id. CONTENT-keyed cache (see _merged_records) so a change is NEVER missed. Returns {} when NEITHER layer is
    present — an absent catalog has no curated records. Corrupt or unreadable
    present layers raise rather than silently exposing stale floor facts."""
    floor_b, legacy_overlay_b, local_b = _catalog_layered_bytes()
    if floor_b is None and legacy_overlay_b is None and local_b is None:
        return {}
    return _merged_records(floor_b, legacy_overlay_b, local_b)


def _bare(model_id):
    """The lookup key: strip a leading 'provider:' namespace. Reasoning suffixes / date snapshots are the caller's to
    normalize (pricing.normalize owns that) — this only removes the vendor prefix so 'deepseek:deepseek-v4-flash' and
    'deepseek-v4-flash' both find the record. No dependency on pricing (circular-import safe)."""
    if not isinstance(model_id, str):
        return ""
    return model_id.split(":", 1)[1] if ":" in model_id else model_id


def model_record(model_id, *, resolve_retired=False, on_synonym_gap=None):
    """The full catalog record for a model, or None if it has no curated record. Tries the id as given, then with the
    'provider:' prefix stripped, then each lane use-name (so 'gemini-3.8-flash-low' finds gemini-3.8-flash). Read-only.

    Callers that already hold a normalized base id (pricing does, before it looks up) get an exact hit; a caller
    passing a lane/suffixed/qualified id is resolved here. A floating alias raises
    LatestGeminiFlashResolutionError when its candidate set cannot resolve uniquely.

    With resolve_retired=True the record's retirement chain (retired_alias_of) is followed to the live canonical
    record; broken or cyclic chains are named gaps (see catalog_record_from_snapshot), never guessed canonicals.
    This is the public gateway path for retirement-aware resolution — callers never reach _load_records directly.
    `on_synonym_gap` is the optional one-message warning sink for a tolerated self-priced retirement gap (see
    catalog_record_from_snapshot): a non-leaf caller passes config.warn_once so this leaf stays config-free."""
    return catalog_record_from_snapshot(
        model_id, _load_records(), resolve_retired=resolve_retired, on_synonym_gap=on_synonym_gap)


def catalog_record_from_snapshot(model_id, models, *, resolve_retired=False, on_synonym_gap=None):
    """Resolve an authored spelling against one captured catalog snapshot.

    `on_synonym_gap`, when given, is called with a single human-readable message the one time a retirement chain
    ends in an unknown target that the retired record itself prices (the self-priced gap below). model_catalog is
    a config-free leaf (see module docstring), so it does not reach spendguard's warning registry itself — the
    non-leaf caller passes config.warn_once as this sink. None (the default) means the gap is tolerated silently."""
    if resolve_retired:
        record = catalog_record_from_snapshot(model_id, models)
        visited = set()
        while record and record.get("retired_alias_of"):
            identifier = record["id"]
            if identifier in visited:
                raise ValueError(f"model synonym gap: cyclic retirement for {model_id!r}")
            visited.add(identifier)
            target = record["retired_alias_of"]
            target_record = catalog_record_from_snapshot(target, models)
            if target_record is None:
                authored_price = price_for_date_from_record(record)
                if authored_price and authored_price.get("in_") is not None:
                    if on_synonym_gap is not None:
                        on_synonym_gap(
                            f"model synonym gap: unknown retirement target {target!r} for {model_id!r}; "
                            f"preserving self-priced catalog identity {identifier!r}")
                    return record
                raise ValueError(f"model synonym gap: unknown retirement target {target!r} for {model_id!r}")
            record = target_record
        return record
    if not model_id:
        return None
    if _bare(model_id) == GEMINI_FLASH_FLOATING_ALIAS:
        return models[latest_gemini_flash_base_id(models)]
    for key in (model_id, _bare(model_id)):
        if key in models:
            return models[key]
    # a lane use-name (…-low/-high) → its base, via the authored lane_spellings (no blind suffix regex)
    bare = _bare(model_id)
    for rid, rec in models.items():
        if bare in (rec.get("aliases") or []):
            return rec
        for spellings in (rec.get("lane_spellings") or {}).values():
            if bare in spellings:
                return rec
    return None


def billing_exempt(model_id):
    """Whether the catalog explicitly declares this model non-billable (never infer from its name)."""
    return bool((model_record(model_id) or {}).get(BILLING_EXEMPT_REASON_FIELD))


def unit_price_table():
    """Catalog-authored non-token rates, overlaid using the same catalog layers as model records."""
    return catalog_pricing_metadata(CATALOG_UNIT_PRICES_FIELD)


def catalog_pricing_metadata(section):
    """Read authored pricing metadata through the catalog's floor and overlay layers."""
    floor_b, overlay_b, local_b = _catalog_layered_bytes()
    floor = _catalog_document_or_empty(floor_b)
    overlay = _catalog_document_or_empty(overlay_b)
    local = _catalog_document_or_empty(local_b)
    return {**floor.get(section, {}), **overlay.get(section, {}), **local.get(section, {})}


def _catalog_document_or_empty(raw):
    if raw is None:
        return {}
    try:
        if raw.lstrip().startswith(b"{"):
            return catalog_document_from_bytes(raw, _overlay_path())
        return _catalog_document_from_sqlite_bytes(raw)
    except CatalogLayerCorruptionError:
        raise
    except (sqlite3.Error, ValueError, TypeError, KeyError, UnicodeError) as exc:
        raise CatalogLayerCorruptionError(f"GAP catalog_layer_corrupt: {exc}") from exc


def cache_modifier_table():
    """Provider-published cache multipliers authored only in the catalog."""
    return catalog_pricing_metadata("cache_modifiers")


def all_records():
    """The whole {id: record} map (a live reference to the memoised dict — treat as read-only)."""
    return _load_records()


def ids():
    """Every curated model id, sorted."""
    return sorted(_load_records().keys())


def _parse_catalog_vendors(raw):
    """The {provider: vendor_record} map from catalog BYTES (the 'vendors' section) — per-vendor metered-API rate-limit
    SSOT. {} for an absent layer; corrupt present layers raise. (Named
    _parse_catalog_vendors, not _parse_vendors, to stay unique vs crossllm._parse_vendors — a different job.)"""
    vendors = _catalog_document_or_empty(raw).get("vendors", {})
    if not isinstance(vendors, dict):
        raise CatalogLayerCorruptionError("GAP catalog_layer_corrupt: vendors must be an object")
    return vendors


@functools.lru_cache(maxsize=8)
def _merged_vendors(floor_b, legacy_overlay_b, local_b):
    """Layer the two catalog blobs' vendors sections (synced T2 overrides the shipped floor T1 by provider),
    content-keyed exactly like _merged_records so a changed byte-blob always re-parses."""
    return {
        **_parse_catalog_vendors(floor_b),
        **_parse_catalog_vendors(legacy_overlay_b),
        **_parse_catalog_vendors(local_b),
    }


def _load_vendors():
    """The {provider: vendor_record} map, layered (shipped floor overlaid by the synced/override catalog). {} when
    neither layer is present — a missing vendors section degrades to 'no curated vendor', never an exception."""
    floor_b, legacy_overlay_b, local_b = _catalog_layered_bytes()
    if floor_b is None and legacy_overlay_b is None and local_b is None:
        return {}
    return _merged_vendors(floor_b, legacy_overlay_b, local_b)


def vendor_record(provider):
    """The catalog's per-vendor metered-API record (rate-limit tiers, cold_floor, header_names, model_family_map), or
    None when the vendor is not curated. Read-only. The SSOT the dispatch governor reads for its PROACTIVE cold cap."""
    if not provider:
        return None
    return _load_vendors().get(str(provider))


def _family_of(provider, model_id):
    """The rate-limit FAMILY bucket `model_id` falls in for `provider` (per the vendor's model_family_map), or None.
    Most-SPECIFIC family wins (so 'opus-5.5' beats 'opus') — matched on the bare id, no suffix regex."""
    fam_map = (vendor_record(provider) or {}).get("model_family_map") or {}
    bare = _bare(model_id)
    hit = None
    for fam, id_list in fam_map.items():
        if bare in (id_list or []) and (hit is None or len(fam) > len(hit)):
            hit = fam
    return hit


def rate_limit_for(model_id, provider=None, tier=None):
    """The PROACTIVE cold-cap dict ({rpm, itpm?, otpm?, tpm?}) for (model, tier) from the catalog vendors section, or
    None when the vendor is not curated (the caller then applies its own default — the catalog asserts no floor it does
    not have). `provider` defaults to the model's curated provider; `tier` to the vendor's cold_tier. For a per-family
    vendor (anthropic) it maps model→family→the tier row, falling to the vendor cold_floor for an unmapped model; for a
    per-key vendor (openai/gemini/zai/moonshot) it returns the cold_floor. The governor uses this as the FLOOR applied
    before any response header, then learns the real ceiling from headers on top. Read-only; never raises."""
    prov = provider or provider_of(model_id)
    rec = vendor_record(prov)
    if not rec:
        return None
    tiers = rec.get("tiers")
    if tiers:
        trow = (tiers.get(tier or rec.get("cold_tier")) or {}).get("models") or {}
        fam = _family_of(prov, model_id)
        if fam and fam in trow:
            return dict(trow[fam])
    return dict(rec["cold_floor"]) if isinstance(rec.get("cold_floor"), dict) else None


def model_price(model_id):
    """The price sub-record {in_, out, cached_in, batch_in, batch_out, [batch_cached_in], source, verified} for a
    model, or None when the model is not in the catalog or has no price (a curated model may carry price_error). This
    is the CURATED rate; pricing.py layers it above the synced breadth cache and does the cost math."""
    rec = model_record(model_id)
    visited = set()
    while rec and not (rec.get("price") or rec.get("price_intervals")) and rec.get("retired_alias_of"):
        target = rec["retired_alias_of"]
        if target in visited:
            return None
        visited.add(target)
        rec = model_record(target)
    return price_for_date_from_record(rec)


def _normalize_catalog_price_query_date(on_date):
    """Normalize a catalog price query date to strict ISO YYYY-MM-DD."""
    target = on_date or datetime.datetime.now(datetime.timezone.utc).date().isoformat()
    if isinstance(target, (datetime.date, datetime.datetime)):
        target = target.date().isoformat() if isinstance(target, datetime.datetime) else target.isoformat()
    if not isinstance(target, str):
        raise ValueError("on_date must be a YYYY-MM-DD date")
    # Validate the WHOLE string (never a [:10] slice, which silently accepts a garbage suffix like
    # '2026-01-01garbage'): a strict ISO date, or a full ISO datetime reduced to its date.
    try:
        try:
            parsed = datetime.date.fromisoformat(target)
        except ValueError:
            parsed = datetime.datetime.fromisoformat(target).date()
    except ValueError as exc:
        raise ValueError("on_date must be a YYYY-MM-DD date") from exc
    return parsed.isoformat()


def price_for_date_from_record(record, on_date=None):
    """Return the catalog rate interval covering ``on_date``; legacy prices are open intervals.

    Intervals are half-open ``[valid_from, valid_until)``.  A legacy ``price`` record is interpreted as
    beginning at its verified date (or the record's as_of date) so existing catalog values migrate without
    inventing a second rate source.
    """
    if not record:
        return None
    intervals = record.get("price_intervals")
    if not intervals:
        legacy = record.get("price")
        if not legacy:
            return None
        interval = dict(legacy)
        interval.setdefault("valid_from", legacy.get("verified") or record.get("as_of"))
        interval.setdefault("valid_until", None)
        if on_date is None and interval["valid_from"] is None:
            return interval
        intervals = [interval]
    target = _normalize_catalog_price_query_date(on_date)
    matches = [interval for interval in intervals
               if interval.get("valid_from") is not None and interval["valid_from"] <= target
               and (interval.get("valid_until") is None or target < interval["valid_until"])]
    if len(matches) > 1:
        raise ValueError(f"overlapping price intervals for {record.get('id')!r} on {target}")
    return dict(matches[0]) if matches else None


def published_ceiling(model_id):
    """The curated published max-OUTPUT-tokens value (int) for a model, or None when unknown/not-curated. The
    RESOLVER (pricing.output_ceiling) reads this first, then live /models, then floors — this is just the datum."""
    rec = model_record(model_id)
    oc = (rec or {}).get("output_ceiling") if rec else None
    v = (oc or {}).get("value")
    try:
        return int(v) if v else None
    except (TypeError, ValueError):
        return None


def context_window(model_id):
    """The curated INPUT context-window size (int tokens) for a model, or None when unknown/not-curated. This is the
    METERED API's window (provider-published, carried WITH source+verified provenance); a LANE's smaller practical
    input capacity is learned separately (resource_state size_ceiling) and must never be conflated with this. The
    datum only — a resolver may layer the synced breadth / a learned floor on top."""
    rec = model_record(model_id)
    cw = (rec or {}).get("context_window") if rec else None
    v = cw.get("value") if isinstance(cw, dict) else None
    try:
        return int(v) if v else None
    except (TypeError, ValueError):
        return None


def embedding_models(provider=None):
    """Every curated EMBEDDING model (capabilities.mode == 'embedding'), optionally filtered to one provider — the SSOT
    for "which models embed", so NO caller hardcodes an embedding model id. Returns [(provider, metered_id)] sorted by
    input price ascending (cheapest first), so a caller wanting one default embedding model per provider takes the first
    for that provider. The `mode` marker is authored in the catalog exactly like a chat model's 'chat' mode; adding a
    provider's embedding model is a catalog ROW (data), never a code literal. Empty when the catalog curates none for the
    filter — an honest absence a caller surfaces, never a silent hardcoded fallback."""
    prov = (provider or "").strip().lower() or None
    rows = []
    for rid, rec in _load_records().items():
        caps = rec.get("capabilities")
        if not isinstance(caps, dict) or caps.get("mode") != "embedding":
            continue
        rp = (rec.get("provider") or "").strip().lower()
        if prov and rp != prov:
            continue
        price_in = (rec.get("price") or {}).get("in_")
        rows.append(((price_in if price_in is not None else 1e9), rec.get("provider"), rec.get("metered_id") or rid))
    rows.sort(key=lambda t: (t[0], str(t[2])))
    return [(p, m) for _in, p, m in rows]


def embedding_input_token_limit(model_id):
    """The curated maximum input tokens for an embedding model, or None when absent/uncurated.

    Embedding records carry their provider-published input limit in ``context_window``.  This accessor narrows that
    general catalog fact to models explicitly authored with ``capabilities.mode == 'embedding'``, so chat windows can
    never be mistaken for an embedding request limit.  The catalog remains the sole owner of the numeric value.
    """
    rec = model_record(model_id)
    caps = (rec or {}).get("capabilities") if rec else None
    if not isinstance(caps, dict) or caps.get("mode") != "embedding":
        return None
    window = (rec or {}).get("context_window")
    value = window.get("value") if isinstance(window, dict) else None
    try:
        return int(value) if value is not None and int(value) > 0 else None
    except (TypeError, ValueError):
        return None


def chat_models(provider=None):
    """Every curated CHAT model (capabilities.mode == 'chat'), optionally filtered to one provider — the SSOT for
    "which models chat", so NO caller hardcodes a chat model id. Returns [(provider, metered_id)] sorted by input price
    ascending (cheapest first). Embedding-only and every other non-chat mode are excluded by the authored catalog mode;
    empty when the catalog curates no chat model for the filter."""
    prov = (provider or "").strip().lower() or None
    rows = []
    for rid, rec in _load_records().items():
        caps = rec.get("capabilities")
        if not isinstance(caps, dict) or caps.get("mode") != "chat":
            continue
        rp = (rec.get("provider") or "").strip().lower()
        if prov and rp != prov:
            continue
        price_in = (rec.get("price") or {}).get("in_")
        rows.append(((price_in if price_in is not None else 1e9), rec.get("provider"), rec.get("metered_id") or rid))
    rows.sort(key=lambda t: (t[0], str(t[2])))
    return [(p, m) for _in, p, m in rows]


def embed_batch_ceiling(model_id):
    """The PROVIDER-ENFORCED maximum number of inputs per /embeddings request for an embedding model, or None when the
    catalog doesn't record one (caller then uses its own default, no clamp). Reads `capabilities.embed_max_batch.value`
    — a per-model fact authored in the catalog (the SSOT), measured not assumed: exceeding it 400s the WHOLE chunk, so
    every input in it fails (a 5,768-text gemini run left 5,760 unembedded under one global default larger than 100).
    Gemini=100, OpenAI=2048, Voyage=1000 (each measured by live bisection). A plain int is also accepted for the field
    (value-only form). None on a missing/malformed entry — never a guessed number."""
    rec = model_record(model_id)
    caps = (rec or {}).get("capabilities") if rec else None
    if not isinstance(caps, dict):
        return None
    emb = caps.get("embed_max_batch")
    v = emb.get("value") if isinstance(emb, dict) else emb          # {value, source} (provenance) or a bare int
    try:
        return int(v) if v is not None and int(v) > 0 else None
    except (TypeError, ValueError):
        return None


def provider_base(provider):
    """The catalog's designated reliable BASE model id for `provider` — the ONE record flagged provider_base:true — or
    None. The tier-3 last-resort fallback target when a chosen model's lane AND metered API both fail
    (adapters.provider_base_model reads this; advisor.provider_base_model config can override per provider).
    Same-provider by construction, so a pinned/consensus call keeps its vendor identity."""
    prov = (provider or "").strip().lower()
    for rid, rec in _load_records().items():
        if (rec.get("provider") or "").strip().lower() == prov and rec.get("provider_base"):
            return rec.get("metered_id") or rid
    return None


def reasoning(model_id):
    """The reasoning sub-record {floor, effort_ok, reasoning_floor, reasons_by_default, tokens_param, style} for a
    model, or None when not curated. Per-model facts only; the lane<->metered effort SPELLING is reasoning_equivalence."""
    rec = model_record(model_id)
    return (rec or {}).get("reasoning") if rec else None


def provider_of(model_id):
    """The vendor that publishes/serves a model per the catalog, or None when not curated."""
    rec = model_record(model_id)
    return (rec or {}).get("provider") if rec else None


def _litellm_breadth_cache():
    """The synced LiteLLM breadth cache ({models, context, capabilities, …} at ~/.spendguard/litellm_prices.json) —
    the documented BREADTH fallback (see module docstring) covering ~3500 models; {} when absent. Read FRESH on each
    call (deliberately NO in-process memo: a cached verdict-source could serve a STALE capability after a sync, and a
    ~2MB parse is ~5ms and only on the FALLBACK path — a curated record short-circuits before this, so the hot 54
    never reach here). HOME is resolved through an injected provider (set_catalog_home_provider) so this leaf never imports config/pricing (no circular import)."""
    home = _resolve_catalog_home()
    try:
        with open(os.path.join(str(home), "litellm_prices.json")) as f:
            return json.load(f) or {}
    except Exception:
        return {}


def _cache_keys_for(model_id, rec):
    """The LiteLLM-cache key candidates for a model — its id, bare id, and (bare) metered_id — deduped, order-preserved.
    The cache is keyed by LiteLLM model names; a caller matches on any of these (mirrors how model_record resolves)."""
    out, seen = [], set()
    for k in (model_id, _bare(model_id), (rec or {}).get("metered_id"), _bare((rec or {}).get("metered_id") or "")):
        if k and k not in seen:
            seen.add(k)
            out.append(k)
    return out


def vision_capable(model_id):
    """Whether a model accepts IMAGE input — True/False/None (None = UNKNOWN, never assumed). Reads the curated
    catalog `vision` bool FIRST (an override), then falls back to the synced LiteLLM cache's supports_vision — so ANY
    model LiteLLM knows (~3500, not just the curated 54) resolves here, i.e. everything we use/might use. Per-model
    TRUTH (sync_capabilities.py sources the catalog flags from LiteLLM; the cache IS LiteLLM), never inferred from the
    id string. A caller routing a vision (images=) call treats NOT-True as 'not known-capable' and keeps its own model
    — so a best-value SWAP on a vision call is allowed ONLY to a model KNOWN to see images (adapters.call). The
    asymmetry is deliberate: a wrong True would send an image to a blind model; a wrong None only keeps the caller's
    (already-vision) model."""
    rec = model_record(model_id)
    v = (rec or {}).get("vision") if rec else None
    if isinstance(v, bool):
        return v
    caps = _litellm_breadth_cache().get("capabilities") or {}
    for key in _cache_keys_for(model_id, rec):
        c = caps.get(key)
        if isinstance(c, dict) and isinstance(c.get("supports_vision"), bool):
            return c["supports_vision"]
    return None


def model_capability(model_id, name):
    """A named boolean capability for a model → True/False/None (unknown). `name` is the SHORT key ('response_schema',
    'function_calling', 'pdf_input', 'prompt_caching', 'tool_choice'); for images use vision_capable(). Reads the
    curated catalog `capabilities` block FIRST, then the LiteLLM cache breadth (where the field is `supports_<name>`) —
    the same catalog-override-then-breadth resolution as vision_capable, so everything we use/might use is covered."""
    rec = model_record(model_id)
    cc = (rec or {}).get("capabilities") if rec else None
    if isinstance(cc, dict) and isinstance(cc.get(name), bool):
        return cc[name]
    caps = _litellm_breadth_cache().get("capabilities") or {}
    for key in _cache_keys_for(model_id, rec):
        c = caps.get(key)
        if isinstance(c, dict) and isinstance(c.get("supports_" + name), bool):
            return c["supports_" + name]
    return None


def as_price_table():
    """The catalog projected in memory: {provider: {model_id: {in_, out, cached_in, batch_in,
    batch_out, [batch_cached_in], _source}}}. The ONE place that knows how a catalog record becomes a price row —
    used by pricing._load to layer the
    catalog's curated rates above the synced breadth. Models with no usable price (price is None or lacks in_) are
    omitted (an unpriced model is a gap, never a $0 row)."""
    out = {}
    for rid, rec in _load_records().items():
        p = price_for_date_from_record(rec) or {}
        if p.get("in_") is None:
            continue
        prov = rec.get("provider") or "?"
        row = {k: p[k] for k in ("in_", "out", "cached_in", "batch_in", "batch_out", "batch_cached_in") if k in p and p[k] is not None}
        src = p.get("source")
        if src:
            row["_source"] = src
        out.setdefault(prov, {})[rec.get("metered_id") or rid] = row
    return out


def _validate_catalog_price_intervals(record_id, intervals):
    """Return contract problems for one record's effective-dated price history."""
    if not isinstance(intervals, list) or not intervals:
        return [f"{record_id}: price_intervals must be a non-empty list"]
    problems = []
    previous_until = None
    for index, interval in enumerate(intervals):
        start, until = interval.get("valid_from"), interval.get("valid_until")
        try:
            start_valid = datetime.date.fromisoformat(start).isoformat() == start
            until_valid = until is None or datetime.date.fromisoformat(until).isoformat() == until
        except (TypeError, ValueError):
            start_valid = until_valid = False
        if not start_valid or not until_valid or (until is not None and start >= until):
            problems.append(f"{record_id}: price_intervals[{index}] has an invalid effective window")
        if previous_until is not None and start < previous_until:
            problems.append(f"{record_id}: price_intervals overlap at index {index}")
        previous_until = until
    if intervals[-1].get("valid_until") is not None:
        problems.append(f"{record_id}: price_intervals has no open current interval")
    return problems


def validate_catalog(models=None, allow_legacy_missing_as_of=True):
    """Check the DATA CONTRACT and return a list of human-readable problems ([] = clean). Used by
    tests/test_model_catalog_ssot.py. Verifies: required top fields present; price fields (when a price exists) are
    numeric or explicitly null; output_ceiling.value is int-or-null; reasoning.style is in REASONING_STYLES; a
    retired_alias_of names a real catalog record OR the alias carries its own price (so the reference never dangles
    into nothing). ``as_of`` is a strict, non-future YYYY-MM-DD. Missing/null dates are an explicit temporary legacy
    rollout exception; pass ``allow_legacy_missing_as_of=False`` after bootstrap to require completeness. Pure; no I/O
    beyond the load."""
    validate_whole_catalog = models is None
    models = models if models is not None else _load_records()
    problems = []
    ids_present = set(models)
    today = datetime.datetime.now(datetime.timezone.utc).date()
    for rid, rec in models.items():
        for f in _REQUIRED_TOP:
            if not rec.get(f):
                problems.append(f"{rid}: missing required field {f!r}")
        as_of = rec.get("as_of")
        if as_of is None:
            if not allow_legacy_missing_as_of:
                problems.append(f"{rid}: missing as_of (legacy rollout exception disabled)")
        elif not isinstance(as_of, str) or not _ISO_DATE.fullmatch(as_of):
            problems.append(f"{rid}: as_of {as_of!r} is not strict YYYY-MM-DD")
        else:
            try:
                parsed_as_of = datetime.date.fromisoformat(as_of)
            except ValueError:
                problems.append(f"{rid}: as_of {as_of!r} is not a real calendar date")
            else:
                if parsed_as_of > today:
                    problems.append(f"{rid}: as_of {as_of!r} is in the future")
        p = rec.get("price")
        if p is not None:
            for k in ("in_", "out", "cached_in", "batch_in", "batch_out"):
                v = p.get(k)
                if v is not None and not isinstance(v, (int, float)):
                    problems.append(f"{rid}: price.{k} is {type(v).__name__}, expected number|null")
        intervals = rec.get("price_intervals")
        if intervals is not None:
            problems.extend(_validate_catalog_price_intervals(rid, intervals))
        oc = (rec.get("output_ceiling") or {}).get("value")
        if oc is not None and not isinstance(oc, int):
            problems.append(f"{rid}: output_ceiling.value is {type(oc).__name__}, expected int|null")
        cw = rec.get("context_window")
        if isinstance(cw, dict):
            cwv = cw.get("value")
            if cwv is not None and not isinstance(cwv, int):
                problems.append(f"{rid}: context_window.value is {type(cwv).__name__}, expected int|null")
        elif cw is not None:
            problems.append(f"{rid}: context_window is {type(cw).__name__}, expected object|null")
        style = (rec.get("reasoning") or {}).get("style")
        if style is not None and style not in REASONING_STYLES:
            problems.append(f"{rid}: reasoning.style {style!r} not in {REASONING_STYLES}")
        caps = rec.get("capabilities")
        if isinstance(caps, dict) and "embed_max_batch" in caps:
            emb = caps["embed_max_batch"]
            emv = emb.get("value") if isinstance(emb, dict) else emb
            if not (isinstance(emv, int) and not isinstance(emv, bool) and emv > 0):
                problems.append(f"{rid}: capabilities.embed_max_batch value is {emv!r}, expected a positive int")
        tgt = rec.get("retired_alias_of")
        if tgt and tgt not in ids_present and not (p and p.get("in_") is not None):
            problems.append(f"{rid}: retired_alias_of {tgt!r} is neither a catalog record nor self-priced (dangling)")
        # as_price_table drops any model with no current `in_` rate; that omission must be TRACEABLE, never silent.
        # A model may be unpriced only by declaring WHY — deliberately billing-exempt, a recorded price gap, or a
        # retired alias that resolves to another record. Resolved via the SAME function as_price_table uses, so the
        # validation rule and the omission can never drift. (An interval contract problem is already reported above;
        # the sentinel avoids double-flagging it here as unpriced.) This is a WHOLE-CATALOG completeness rule: it
        # mirrors as_price_table, which projects only the shipped catalog (_load_records). A targeted call that passes
        # synthetic records is exercising the per-record rules above (e.g. as_of), not the price-table projection, so
        # a price-less stub there is legitimately unpriced and must NOT be flagged — hence the whole-catalog gate.
        if validate_whole_catalog:
            try:
                effective_in = (price_for_date_from_record(rec) or {}).get("in_")
            except ValueError:
                effective_in = "unresolved"
            if effective_in is None and not rec.get("billing_exempt_reason") and not rec.get("price_error") \
                    and not rec.get("retired_alias_of"):
                problems.append(
                    f"{rid}: unpriced (no current in_ rate) and declares no billing_exempt_reason, price_error, "
                    f"or retired_alias_of — as_price_table would drop it from the price table without a trace")
    # provider_base: at most ONE per provider, and a flagged base must be PRICED (a last-resort fallback must be usable)
    bases = {}
    for rid, rec in models.items():
        if rec.get("provider_base"):
            bases.setdefault(rec.get("provider"), []).append(rid)
            if (rec.get("price") or {}).get("in_") is None:
                problems.append(f"{rid}: flagged provider_base but is unpriced (a base must be usable)")
    for prov, rids in bases.items():
        if len(rids) > 1:
            problems.append(f"provider {prov!r} has {len(rids)} provider_base records {rids}; expected exactly one")
    return problems


class CatalogLayerCorruptionError(ValueError):
    """A catalog layer cannot be trusted as complete JSON."""


def catalog_document_from_bytes(raw, path):
    """Parse a whole layer, naming corruption rather than hiding missing facts."""
    try:
        document = json.loads(raw) if raw is not None else {}
        if not isinstance(document, dict):
            raise ValueError("catalog document must be an object")
        return document
    except CatalogLayerCorruptionError:
        raise
    except (ValueError, UnicodeError) as exc:
        raise CatalogLayerCorruptionError(f"GAP catalog_layer_corrupt: {path}: {exc}") from exc


def _catalog_bundle_paths(layer):
    """Private physical layout for one catalog generation."""
    if layer == _CATALOG_LAYER_SEED:
        directory = Path(_CATALOG_WORKSPACE.get() or _HERE)
        database = directory / Path(_CATALOG_DATABASE_PATH).name if _CATALOG_WORKSPACE.get() else Path(_CATALOG_DATABASE_PATH)
        return directory / _CATALOG_JSON_FILENAME, directory / _CATALOG_SEED_FILENAME, database
    if layer != _CATALOG_LAYER_LOCAL:
        raise ValueError(f"unknown catalog authoring layer: {layer!r}")
    source = Path(catalog_authoring_path())
    return source, source.with_suffix(_CATALOG_LOCAL_SEED_SUFFIX), Path(_local_database_path())


def _catalog_atomic_bytes(destination, payload):
    """Replace one file with flushed bytes, retaining its permissions."""
    staged = None
    try:
        with tempfile.NamedTemporaryFile(dir=destination.parent, prefix=destination.name,
                                         suffix=_CATALOG_STAGE_SUFFIX, delete=False) as stream:
            staged = Path(stream.name)
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        if destination.exists():
            os.chmod(staged, destination.stat().st_mode)
        os.replace(staged, destination)
    finally:
        if staged is not None:
            staged.unlink(missing_ok=True)


def _recover_catalog_bundle(paths):
    """Recover an interrupted generation before exposing any layer to a reader."""
    journal = Path(str(paths[0]) + _CATALOG_TRANSACTION_SUFFIX)
    if not journal.exists():
        return
    originals = json.loads(journal.read_bytes())
    if set(originals) != {str(path) for path in paths}:
        raise CatalogLayerCorruptionError(f"GAP catalog_transaction_corrupt: {journal}")
    for path in paths:
        previous = originals[str(path)]
        original = base64.b64decode(previous, validate=True) if previous is not None else None
        current = path.read_bytes() if path.exists() else None
        if current == original:
            continue
        if original is None:
            path.unlink(missing_ok=True)
        else:
            _catalog_atomic_bytes(path, original)
    _catalog_sync_directory(paths[0].parent)
    journal.unlink()
    _catalog_sync_directory(paths[0].parent)
    _clear_catalog_generation_caches()


@contextlib.contextmanager
def _catalog_layer_lock(layer, *, authoring=False):
    """Lock the stable directory inode, including the very first read/write.

    Directory locking needs no file creation on read-only package installs and
    cannot race the creation of a separate lock file by the first writer.
    """
    paths = _catalog_bundle_paths(layer)
    if authoring:
        paths[0].parent.mkdir(parents=True, exist_ok=True)
    directory_fd = os.open(paths[0].parent, os.O_RDONLY)
    try:
        fcntl.flock(directory_fd, fcntl.LOCK_EX if authoring else fcntl.LOCK_SH)
        journal = Path(str(paths[0]) + _CATALOG_TRANSACTION_SUFFIX)
        if journal.exists():
            fcntl.flock(directory_fd, fcntl.LOCK_EX)
            _recover_catalog_bundle(paths)
        yield paths
    finally:
        os.close(directory_fd)


def _catalog_layered_bytes():
    """Capture complete generations for every logical reader, recovering first."""
    with _catalog_layer_lock(_CATALOG_LAYER_SEED):
        floor = _floor_bytes()
    try:
        with _catalog_layer_lock(_CATALOG_LAYER_LOCAL):
            overlay = _read_bytes(_overlay_path())
            local = _read_bytes(_local_database_path())
    except FileNotFoundError:
        # A nonexistent home has no local generation. No file observations are
        # performed after that absence, so a concurrent first author is safe.
        overlay, local = None, None
    return floor, overlay, local


def _clear_catalog_generation_caches():
    _floor_bytes_from_packaged_seed.cache_clear()
    _merged_records.cache_clear()
    _merged_vendors.cache_clear()


def _catalog_sync_directory(directory):
    """Persist transaction journal installation and generation commit ordering."""
    directory_fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def _prepare_catalog_bundle(document):
    """Generate and verify every byte before publishing a catalog generation."""
    directory = Path(_CATALOG_WORKSPACE.get() or _HERE)
    schema = (directory / _CATALOG_SCHEMA_FILENAME).read_text(encoding=_CATALOG_ENCODING)
    seed = render_catalog_seed(document, schema, provenance_authored=PROVENANCE_AUTHORED,
                               provenance_sourced=PROVENANCE_SOURCED)
    connection = sqlite3.connect(_CATALOG_MEMORY_DATABASE)
    try:
        connection.executescript(seed)
        database = connection.serialize()
    finally:
        connection.close()
    reconstructed = _catalog_document_from_sqlite_bytes(database)
    if reconstructed != document:
        raise ValueError(f"catalog generated parity failure: {_catalog_differing_cells(document, reconstructed)}")
    encoded = (json.dumps(document, indent=_CATALOG_JSON_INDENT, ensure_ascii=False) + "\n").encode(_CATALOG_ENCODING)
    return encoded, seed.encode(_CATALOG_ENCODING), database


def _publish_catalog_bundle(paths, payloads):
    """Publish under the layer lock with a durable undo journal for crash recovery.

    SQLite replacement is atomic. Gateway multi-file readers hold the same lock;
    a failure or process death between replacements restores the entire previous
    generation on the next gateway access, before returning any records.
    """
    originals = {str(path): base64.b64encode(path.read_bytes()).decode(_CATALOG_BASE64_ENCODING) if path.exists() else None
                 for path in paths}
    source = paths[0]
    if source.exists():
        backup = Path(str(source) + _CATALOG_BACKUP_SUFFIX)
        backup.write_bytes(source.read_bytes())
        if backup.read_bytes() != source.read_bytes():
            raise OSError("catalog snapshot verification failed")
    journal = Path(str(source) + _CATALOG_TRANSACTION_SUFFIX)
    _catalog_atomic_bytes(journal, json.dumps(originals).encode(_CATALOG_ENCODING))
    _catalog_sync_directory(source.parent)
    try:
        for path, payload in zip(paths, payloads):
            _catalog_atomic_bytes(path, payload)
        _catalog_sync_directory(source.parent)
        journal.unlink()
        _catalog_sync_directory(source.parent)
    except BaseException:
        _recover_catalog_bundle(paths)
        raise
    finally:
        _clear_catalog_generation_caches()


def author_catalog_records(mutator, *, reason, layer=_CATALOG_LAYER_LOCAL):
    """Atomically author a catalog, regenerating its SQL and SQLite with parity.

    ``mutator(document, snapshot)`` runs under the writer lock. ``document`` is
    the chosen author's whole JSON document and ``snapshot`` is the full logical
    model map including the floor. Return the new document (or mutate in place).
    Local authors preserve the immutable shipped floor; ``layer='seed'`` is the
    explicit maintainers' operation for updating the reviewable packaged seed.
    Returns the authoring destination for display by existing CLI callers.
    """
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError("catalog authoring reason is required")
    with _catalog_layer_lock(layer, authoring=True) as paths:
        original = paths[0].read_bytes() if paths[0].exists() else None
        if layer == _CATALOG_LAYER_SEED and original is None:
            raise CatalogLayerCorruptionError(f"GAP catalog_seed_missing: {paths[0]}")
        document = catalog_document_from_bytes(original, paths[0])
        records = document.setdefault("models", {})
        if not isinstance(records, dict):
            raise ValueError("catalog models must be an object")
        if layer == _CATALOG_LAYER_SEED:
            floor = records
        else:
            with _catalog_layer_lock(_CATALOG_LAYER_SEED):
                floor = _catalog_document_or_empty(_floor_bytes()).get("models", {})
        snapshot = copy.deepcopy({**floor, **records})
        working = copy.deepcopy(document)
        result = mutator(working, snapshot)
        document = working if result is None else result
        payloads = _prepare_catalog_bundle(document)
        _publish_catalog_bundle(paths, payloads)
    return str(paths[0])


def catalog_seed_document():
    """A detached snapshot of the complete reviewable authoring seed."""
    with _catalog_layer_lock(_CATALOG_LAYER_SEED) as paths:
        return catalog_document_from_bytes(paths[0].read_bytes(), paths[0])


def catalog_floor_document():
    """The shipped SQLite floor reconstructed without local overlays."""
    with _catalog_layer_lock(_CATALOG_LAYER_SEED):
        return copy.deepcopy(_catalog_document_or_empty(_floor_bytes()))


def _catalog_differing_cells(expected, actual, prefix=""):
    if isinstance(expected, dict) and isinstance(actual, dict):
        differences = []
        for key in sorted(expected.keys() | actual.keys()):
            path = f"{prefix}.{key}" if prefix else str(key)
            if key not in expected or key not in actual:
                differences.append(path)
            else:
                differences.extend(_catalog_differing_cells(expected[key], actual[key], path))
        return differences
    return [] if expected == actual else [prefix]


def catalog_seed_parity(*, layer=_CATALOG_LAYER_SEED):
    """Compare every cell of JSON, SQL reconstruction and the physical artifact."""
    with _catalog_layer_lock(layer) as paths:
        document = catalog_document_from_bytes(paths[0].read_bytes(), paths[0])
        connection = sqlite3.connect(_CATALOG_MEMORY_DATABASE)
        try:
            connection.executescript(paths[1].read_text(encoding=_CATALOG_ENCODING))
            from_seed = _catalog_document_from_sqlite_bytes(connection.serialize())
        finally:
            connection.close()
        if paths[2].exists():
            database = paths[2].read_bytes()
        elif layer == _CATALOG_LAYER_SEED:
            database = _floor_bytes_from_packaged_seed()
        else:
            raise CatalogLayerCorruptionError(f"GAP catalog_database_missing: {paths[2]}")
        from_database = _catalog_document_from_sqlite_bytes(database)
        differences = [f"seed:{cell}" for cell in _catalog_differing_cells(document, from_seed)]
        differences.extend(f"sqlite:{cell}" for cell in _catalog_differing_cells(document, from_database))
        return {"model_count": len(document.get("models", {})), "differing_cells": differences}


@contextlib.contextmanager
def catalog_source_override(document):
    """Temporarily inject a logical floor snapshot, without physical handles."""
    token = _CATALOG_SOURCE_OVERRIDE.set(copy.deepcopy(document))
    try:
        yield
    finally:
        _CATALOG_SOURCE_OVERRIDE.reset(token)


@contextlib.contextmanager
def catalog_authoring_workspace(directory, *, document=None):
    """Use a complete isolated seed workspace, primarily for offline verification."""
    snapshot = catalog_seed_document() if document is None else copy.deepcopy(document)
    schema_directory = Path(_CATALOG_WORKSPACE.get() or _HERE)
    schema = (schema_directory / _CATALOG_SCHEMA_FILENAME).read_bytes()
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    token = _CATALOG_WORKSPACE.set(str(directory))
    try:
        (directory / _CATALOG_SCHEMA_FILENAME).write_bytes(schema)
        payloads = _prepare_catalog_bundle(snapshot)
        for path, payload in zip(_catalog_bundle_paths(_CATALOG_LAYER_SEED), payloads):
            path.write_bytes(payload)
        _clear_catalog_generation_caches()
        yield
    finally:
        _CATALOG_WORKSPACE.reset(token)
        _clear_catalog_generation_caches()
