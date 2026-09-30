"""model_catalog — THE single authoring home and read API for per-model data (concern 'model_catalog' in
docs/CANONICAL_CONCERNS.json). One typed record per model, authored in the co-located model_catalog.json.

WHY THIS EXISTS: model facts were spread across six owner modules (pricing, models, catalog, lane_registry,
lane_catalog, reasoning_equivalence) + config + scattered literals, with no single per-model record — so a fresh
env had gemini/deepseek unpriced (they lived only in the synced LiteLLM cache), and a model id / price / ceiling /
reasoning-floor could be authored in several places at once. This module is the ONE place a model fact is authored.

SoT ≠ one COPY: derived copies are fine so long as they are generated + fresh + never hand-edited. prices.json is a
GENERATED projection of this catalog (it carries a _generated marker); the synced LiteLLM cache is a BREADTH fallback
for models not in the catalog. PUBLISHED fields (price, output_ceiling) carry the PROVIDER as origin via a `source` +
`verified` provenance; INTERNAL decisions (lane<->metered spelling, chosen reasoning floor) are authored here.

Record schema (the DATA CONTRACT — see validate()):
  id: str (stable key)                      provider: str
  aliases: [str]                            retired_alias_of: str | null
  metered_id: str                           lane_spellings: {lane: [use-name, ...]}
  price: {in_, out, cached_in, batch_in, batch_out, batch_cached_in?, source, verified} | null   price_error: str | null
  output_ceiling: {value: int|null, source: str|null}     context_window: {value: int|null, source, verified} | null
  provider_base: true (ONLY on the one reliable base model per provider — the tier-3 fallback target; absent else)
  reasoning: {floor: str|null, effort_ok: bool, reasoning_floor: str|null, reasons_by_default: bool,
              tokens_param: str, style: one of REASONING_STYLES}

No dependency on pricing/models (avoids a circular import — pricing reads THIS at load). $0, read-only."""
import functools
import json
import os

_HERE = os.path.dirname(os.path.abspath(__file__))
DATA_PATH = os.path.join(_HERE, "model_catalog.json")

REASONING_STYLES = ("param", "suffix", "thinking", "none")   # enum domain for reasoning.style (DATA_CONTRACT)
_REQUIRED_TOP = ("id", "provider")                            # a record must at least identify itself + its vendor


def _overlay_path():
    """The SERVER-SYNCED / local-override curated catalog overlay — a {"models": {id: record}} file in SPENDGUARD_HOME,
    the SAME shape as the shipped floor (DATA_PATH). Written by `spendguard sync-catalog` from the server's curated
    catalog (the data-plane T2 layer); a user may also drop it by hand to OVERRIDE the shipped floor locally. Distinct
    filename from catalog.py's live served-list cache (HOME/model_catalog.json), a different store. None on any error
    → floor only (config is imported lazily, as elsewhere in this leaf, to avoid a load-time cycle)."""
    try:
        from . import config
        return os.path.join(str(config.HOME), "catalog_synced.json")
    except Exception:
        return None


def _read_bytes(path):
    """The file's raw bytes, or None if absent/unreadable (a missing/unreadable layer degrades to None; the merge
    treats it as empty, never raises)."""
    if not path:
        return None
    try:
        with open(path, "rb") as f:
            return f.read()
    except OSError:
        return None


def _parse_models(raw):
    """The {id: record} map from catalog BYTES, or {} for empty/corrupt input — a corrupt layer degrades to empty and
    never wipes the other in the merge. Mechanical parse of a fixed {"models": {...}} shape."""
    if not raw:
        return {}
    try:
        return (json.loads(raw) or {}).get("models") or {}
    except Exception:
        return {}


@functools.lru_cache(maxsize=8)
def _merged_records(floor_b, ov_b):
    """Parse + LAYER the two catalog byte-blobs: the shipped curated floor (T1) OVERLAID by the synced/override layer
    (T2), synced records winning by model id. Keyed by the raw BYTES, so the parse+merge is memoised per exact CONTENT
    — a restore / rsync / iCloud sync / touch that installs DIFFERENT content (even same-size, even with a reset
    mtime) still re-parses, because the bytes (the cache key) differ. There is no mtime/size reliance to go stale.
    ≤8 blobs held; the hot accessor path re-parses only on a genuine content change."""
    return {**_parse_models(floor_b), **_parse_models(ov_b)}


def _load_records():
    """The {id: record} map, LAYERED per the data plane: the shipped curated floor (T1) OVERLAID by the server-synced
    / local-override catalog (SPENDGUARD_HOME/catalog_synced.json, T2) where present — synced records override/add by
    model id. CONTENT-keyed cache (see _merged_records) so a change is NEVER missed. Returns {} when NEITHER layer is
    present/readable — a missing catalog degrades to 'no curated record' (callers fall back to the synced breadth),
    never an exception."""
    floor_b = _read_bytes(DATA_PATH)
    ov_b = _read_bytes(_overlay_path())
    if floor_b is None and ov_b is None:
        return {}
    return _merged_records(floor_b, ov_b)


def _bare(model_id):
    """The lookup key: strip a leading 'provider:' namespace. Reasoning suffixes / date snapshots are the caller's to
    normalize (pricing.normalize owns that) — this only removes the vendor prefix so 'deepseek:deepseek-v4-flash' and
    'deepseek-v4-flash' both find the record. No dependency on pricing (circular-import safe)."""
    if not isinstance(model_id, str):
        return ""
    return model_id.split(":", 1)[1] if ":" in model_id else model_id


def model_record(model_id):
    """The full catalog record for a model, or None if it has no curated record. Tries the id as given, then with the
    'provider:' prefix stripped, then each lane use-name (so 'gemini-3.8-flash-low' finds gemini-3.8-flash). Read-only.

    Callers that already hold a normalized base id (pricing does, before it looks up) get an exact hit; a caller
    passing a lane/suffixed/qualified id is resolved here. Never raises."""
    models = _load_records()
    if not model_id:
        return None
    for key in (model_id, _bare(model_id)):
        if key in models:
            return models[key]
    # a lane use-name (…-low/-high) → its base, via the authored lane_spellings (no blind suffix regex)
    bare = _bare(model_id)
    for rid, rec in models.items():
        for spellings in (rec.get("lane_spellings") or {}).values():
            if bare in spellings:
                return rec
    return None


def all_records():
    """The whole {id: record} map (a live reference to the memoised dict — treat as read-only)."""
    return _load_records()


def ids():
    """Every curated model id, sorted."""
    return sorted(_load_records().keys())


def model_price(model_id):
    """The price sub-record {in_, out, cached_in, batch_in, batch_out, [batch_cached_in], source, verified} for a
    model, or None when the model is not in the catalog or has no price (a curated model may carry price_error). This
    is the CURATED rate; pricing.py layers it above the synced breadth cache and does the cost math."""
    rec = model_record(model_id)
    return (rec or {}).get("price") if rec else None


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
    never reach here). config is imported LAZILY so this leaf never load-imports config/pricing (no circular import)."""
    try:
        from . import config
        with open(os.path.join(str(config.HOME), "litellm_prices.json")) as f:
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
    """The catalog projected into the prices.json shape: {provider: {model_id: {in_, out, cached_in, batch_in,
    batch_out, [batch_cached_in], _source}}}. The ONE place that knows how a catalog record becomes a price row —
    used by scripts/gen_prices_json.py to GENERATE prices.json (a derived artifact) and by pricing._load to layer the
    catalog's curated rates above the synced breadth. Models with no usable price (price is None or lacks in_) are
    omitted (an unpriced model is a gap, never a $0 row)."""
    out = {}
    for rid, rec in _load_records().items():
        p = rec.get("price") or {}
        if p.get("in_") is None:
            continue
        prov = rec.get("provider") or "?"
        row = {k: p[k] for k in ("in_", "out", "cached_in", "batch_in", "batch_out", "batch_cached_in") if k in p and p[k] is not None}
        src = p.get("source")
        if src:
            row["_source"] = src
        out.setdefault(prov, {})[rec.get("metered_id") or rid] = row
    return out


def validate_catalog(models=None):
    """Check the DATA CONTRACT and return a list of human-readable problems ([] = clean). Used by
    tests/test_model_catalog_ssot.py. Verifies: required top fields present; price fields (when a price exists) are
    numeric or explicitly null; output_ceiling.value is int-or-null; reasoning.style is in REASONING_STYLES; a
    retired_alias_of names a real catalog record OR the alias carries its own price (so the reference never dangles
    into nothing). Pure; no I/O beyond the load."""
    models = models if models is not None else _load_records()
    problems = []
    ids_present = set(models)
    for rid, rec in models.items():
        for f in _REQUIRED_TOP:
            if not rec.get(f):
                problems.append(f"{rid}: missing required field {f!r}")
        p = rec.get("price")
        if p is not None:
            for k in ("in_", "out", "cached_in", "batch_in", "batch_out"):
                v = p.get(k)
                if v is not None and not isinstance(v, (int, float)):
                    problems.append(f"{rid}: price.{k} is {type(v).__name__}, expected number|null")
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


