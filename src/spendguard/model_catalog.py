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
import json
import os
import threading

_HERE = os.path.dirname(os.path.abspath(__file__))
DATA_PATH = os.path.join(_HERE, "model_catalog.json")

REASONING_STYLES = ("param", "suffix", "thinking", "none")   # enum domain for reasoning.style (DATA_CONTRACT)
_REQUIRED_TOP = ("id", "provider")                            # a record must at least identify itself + its vendor

_lock = threading.Lock()
_MEM = {"mtime": None, "models": None}


def _load_records():
    """The {id: record} map from model_catalog.json, memoised by file mtime (re-reads only when the file changes).
    Returns {} if the file is absent/unreadable — a missing catalog degrades to 'no curated record' (callers fall back
    to the synced breadth), never an exception."""
    try:
        st = os.stat(DATA_PATH)
    except OSError:
        return {}
    with _lock:
        if _MEM["mtime"] != st.st_mtime:
            try:
                with open(DATA_PATH) as f:
                    _MEM["models"] = (json.load(f) or {}).get("models") or {}
                _MEM["mtime"] = st.st_mtime
            except Exception:
                return _MEM["models"] or {}
        return _MEM["models"] or {}


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
