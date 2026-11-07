"""Resolve model ids and provider registry operations, assemble SDK clients and timeouts, crosswalk metered and served ids, transform Gemini reasoning-tier ids, report result provenance, and apply the prompt-cache threshold."""
import os

from ... import config
from spendguard.store.configuration import provider_key_env_registry as _pkenv
from spendguard.store.reference_data import reference_data_accessors as _reference_data_accessors

from .execution_accessors import PROVIDERS, SPENDGUARD_IMPORT_NAME


def llm_capability_import_names() -> frozenset[str]:
    """Top-level imports that mechanically establish an LLM-calling capability."""
    names = {SPENDGUARD_IMPORT_NAME}
    for provider in PROVIDERS.values():
        kind = provider.get("kind")
        if kind in ("openai", "anthropic"):
            names.add(kind)
        if provider.get("sdk_import"):
            names.add(provider["sdk_import"])
    return frozenset(names)


def register_provider(name, base_url, key_env, prefixes, kind="openai", sdk_import=None):
    """Add/override a provider. kind: 'openai' (OpenAI-compatible) or 'anthropic'."""
    entry = {"base_url": base_url, "key_env": key_env, "prefixes": tuple(prefixes), "kind": kind}
    if sdk_import:
        entry["sdk_import"] = sdk_import
    PROVIDERS[name] = entry
    _pkenv.register_runtime_provider_key_env(name, key_env)


def provider_for(model):
    """Resolve provider from a model id. Accepts explicit 'provider:model' too. When several providers' prefixes
    match, the LONGEST (most specific) prefix wins — DETERMINISTIC, independent of PROVIDERS insertion order, so a
    plugin whose prefix overlaps a built-in can't resolve by dict order and silently bill the wrong vendor."""
    if not isinstance(model, str) or not model.strip():
        raise ValueError(f"model must be a non-empty id or 'provider:model' — got {model!r}")
    if ":" in model:
        return model.split(":", 1)[0]
    best_name, best_len = None, -1
    for name, p in PROVIDERS.items():
        prefixes = p["prefixes"]
        for pref in (prefixes if isinstance(prefixes, (tuple, list)) else (prefixes,)):
            if model.startswith(pref) and len(pref) > best_len:
                best_name, best_len = name, len(pref)
    if best_name is not None:
        return best_name
    raise ValueError(f"unknown provider for model {model!r} — use 'provider:model' or register_provider()")


def provider_base_model(provider):
    """The RELIABLE BASE model for `provider` — the tier-3 last-resort fallback when a chosen model's $0 lane AND its
    metered API both fail. Resolution: advisor.provider_base_model[provider] (an operator OVERRIDE) wins, else the
    catalog's designated base (model_catalog.provider_base — the SSOT default). None if neither → tier-3 is a no-op for
    that provider and the chain ends at metered. Never a hardcoded id here (the config/catalog own it), so a stale id
    can't be baked in; same-provider by construction, so a consensus panel keeps its vendor identity."""
    prov = (provider or "").strip().lower()
    try:
        from ... import config as _cfg
        m = (_cfg._cfg_get("advisor", "provider_base_model", {}) or {}).get(prov)
        if m:
            return m
    except Exception:
        pass
    try:
        from ... import model_catalog as _mc
        return _mc.provider_base(prov) or None
    except Exception:
        return None


def safe_input_chars(model, intent=None, reply_tokens=8000, prompt_tokens=1500, chars_per_token=3.0):
    """Max INPUT characters that safely FIT the context window (leaving room for the reply + the prompt scaffold) — so
    a caller that SEGMENTS its own input (e.g. 7thsense's document comprehender) sizes to the REAL, catalog-sourced
    window instead of a hardcoded guess. When `intent` is given, the MIN context window across the configured candidate
    models (advisor.tiers) is used, so ANY model best-value might pick still fits. Sourced from
    model_catalog.context_window (the METERED window); a LANE's smaller practical input capacity is a separate, LEARNED
    axis (resource_state size_ceiling) with the tier-3 fallback behind it — so this is the upper SAFETY bound, not a
    lane-usage target. Returns None when NO context is known (the caller keeps its own conservative default — never a
    guessed number). chars_per_token is deliberately conservative (3.0; English prose is ~3.6+), so the estimate never
    overshoots a real window."""
    from ... import model_catalog as _mc
    ctxs = []
    cw = _mc.context_window(model)
    if cw:
        ctxs.append(int(cw))
    if intent:
        try:
            tiers = config._cfg_get("advisor", "tiers", {}) or {}
            for _models in tiers.values():
                for _m in ([_models] if isinstance(_models, str) else (_models or [])):
                    c = _mc.context_window(_m)
                    if c:
                        ctxs.append(int(c))
        except Exception:
            pass
    if not ctxs:
        return None
    usable = max(0, min(ctxs) - int(reply_tokens) - int(prompt_tokens))
    return int(usable * float(chars_per_token))


def _sdk_client_max_retries(timeout_s):
    # TEST/OPS knob makes offline tests fail-fast against the dead proxy; unset = production identical.
    retry_override = os.getenv("SPENDGUARD_SDK_MAX_RETRIES")
    if retry_override is not None:
        try:
            retry_count = int(retry_override)
        except ValueError:
            raise ValueError("SPENDGUARD_SDK_MAX_RETRIES must be a non-negative integer") from None
        if retry_count < 0:
            raise ValueError("SPENDGUARD_SDK_MAX_RETRIES must be a non-negative integer")
        return retry_count
    return 0 if timeout_s else None


def _oai_compat_client(provider, timeout_s=None):
    """The OpenAI-SDK client for an OpenAI-COMPATIBLE provider — the ONE place the per-provider base_url + key +
    fast-connect discipline is assembled, so completions AND embeddings build the client identically (no drift).
    Raises for an unknown provider, a non-OpenAI-compatible one, or a missing key."""
    spec = PROVIDERS.get(provider)
    if not spec:
        raise ValueError(f"unknown provider {provider!r}")
    if spec.get("kind") != "openai":
        raise ValueError(f"{provider} is not OpenAI-compatible (kind={spec.get('kind')!r}) — no /embeddings shape")
    key = config.api_key(spec["key_env"])
    if not key:
        raise ValueError(f"no key ({spec['key_env']}) for {provider}")
    import openai
    OpenAI = openai.OpenAI
    openai_compat_sdk_base_client = getattr(openai, "_base_client", None)
    compat_retry_count = _sdk_client_max_retries(timeout_s)
    compat_client_options = {} if compat_retry_count is None else {"max_retries": compat_retry_count}
    if timeout_s:
        compat_client_options["timeout"] = _http_timeout(timeout_s, openai_compat_sdk_base_client)
    return OpenAI(api_key=key, base_url=spec["base_url"], **compat_client_options)


def deadline_for(model, intent=None, in_chars=None, default_s=None):
    """PUBLIC deadline advisor — how many seconds a call should be ALLOWED to take, sized from MEASURED latency
    (never a hardcoded number). Returns (seconds, basis): `seconds` is a proposed deadline you pass as timeout_s;
    `basis` names where it came from — 'measured:class(n=…)' / 'measured:model(n=…)' / 'caller' / 'lane-floor' /
    'unknown'. `seconds` is None with basis 'unknown' when there is not yet enough measurement (>= 5 obs) — answer
    that with your own default_s rather than have a guess invented for you. Clamped to [DEADLINE_FLOOR_S,
    DEADLINE_CEIL_S] (30s..1800s) and floored to a lane's minimum when a $0 lane serves the vendor.

    This is the ONE public door onto the internal sizing (vendor_call.time_budget), so a consumer never imports
    that internal: pass the model (a bare id or 'provider:model'), the job `intent`, and the input size in chars;
    the vendor and call-class are derived here. Read-only, $0 — no model call. NOTE: adapters.call and crossllm.ask
    already APPLY this sizing transitively — use this only to SIZE a deadline you pass yourself (e.g. a Batch submit)."""
    try:
        from . import vendor_call
        prov = model.split(":", 1)[0] if ":" in model else provider_for(model)
        mid = model.split(":", 1)[1] if ":" in model else model
        sig = vendor_call.class_sig(mid, intent) if intent else None
        return vendor_call.time_budget(prov, mid, sig=sig, default_s=default_s, in_chars=in_chars)
    except Exception:
        return (float(default_s), "caller") if default_s else (None, "unknown")


def was_substituted(result):
    """True if a DIFFERENT model answered this call than was requested — a lane-bandit or confirmed-substitute swap.
    The provenance is `result['substituted_from']` (the model you asked for); `result['model']` is who answered."""
    return bool(result.get("substituted_from"))


def served_by(result):
    """The vendor that ACTUALLY answered — read from the RESULT, never from what was requested. When the bandit
    swapped the model this is the SUBSTITUTE's vendor, not the one you asked for. A cross-vendor panel MUST key on
    this, never on the requested model: the lane bandit (bandit_mode=optout) can run one model 'in place of' four,
    and every result still carries its REQUESTED label — so keying on the request reads a collapse as four agreeing
    vendors (measured: gpt-5.5 answering as anthropic/gemini/zai/moonshot, panel printed all-ok). Uses the recorded
    answering model (the honesty invariant: recorded model == who answered), falling back to the executor/lane."""
    m = result.get("model")
    if m:
        try:
            return provider_for(m)
        except Exception:
            pass
    return result.get("executor") or "?"


def panel_providers(results):
    """The set of vendors that ACTUALLY answered a group of calls meant to be a cross-vendor panel — so a caller can
    assert diversity FROM THE RESULTS (`len(panel_providers(rs)) == n`), never assume it. If the bandit collapsed the
    panel this set is smaller than the number of successful calls, even though every result is labelled with a
    different requested model. Errored results are excluded (a vendor that failed did not answer)."""
    return {served_by(r) for r in results if not r.get("error")}


CONNECT_TIMEOUT_S = 10.0     # a live vendor's TCP+TLS handshake is well under this; a blackholed one must fail
                             # HERE, fast — not after tying up a panel slot for the whole read budget.
def _sdk_timeout_cls(sdk_base_client):
    """Resolve the Timeout class from the transport module imported by an SDK client.

    The transport is identified by its module type and HTTP client surface rather than by package name, so an
    SDK can change transport packages without requiring a matching change here. Returns None when the SDK base
    client does not expose a matching transport module.
    """
    import types
    if not isinstance(sdk_base_client, types.ModuleType):
        return None
    for sdk_base_client_value in vars(sdk_base_client).values():
        if isinstance(sdk_base_client_value, types.ModuleType) and all(
            hasattr(sdk_base_client_value, transport_attribute)
            for transport_attribute in ("Timeout", "URL", "HTTPTransport")
        ):
            return sdk_base_client_value.Timeout
    return None


def _http_timeout(timeout_s, sdk_base_client):
    """An SDK-native Timeout that fails FAST on a DEAD endpoint but gives a live one the full read budget.

    A bare float handed to the SDK sets connect == read == write == budget, so a vendor that BLACKHOLES the
    connection (measured: z.ai, SYN dropped) held a fan_out slot for the entire budget — up to 600s — just
    trying to connect, and one such vendor made every file in the panel wait that long. Splitting the two so
    CONNECT is short and READ is the budget lets a down vendor be declared unavailable in ~10s while a genuinely
    slow-but-alive one still gets its measured time. Falls back to the plain float if the SDK base client does
    not expose its transport module, allowing the SDK to coerce the timeout itself."""
    if not timeout_s:
        return None
    timeout_cls = _sdk_timeout_cls(sdk_base_client)
    if timeout_cls is None:
        return float(timeout_s)
    return timeout_cls(float(timeout_s), connect=min(CONNECT_TIMEOUT_S, float(timeout_s)))


# TWO SPELLINGS OF ONE MODEL. agy (the Gemini subscription lane) names reasoning EFFORT as a trailing model-id
# suffix — gemini-3.7-flash-{low,medium,high}, gemini-3.1-pro-{low,high} (see `agy models`). The METERED Gemini
# API names the BARE model (gemini-3.7-flash) and takes effort as a reasoning PARAMETER. So the same logical model
# is spelled one way for the lane and another for the API, and a call that crosses namespaces must be RESPELLED —
# otherwise the tier is silently dropped on the lane, or an agy suffix id is handed to the metered API and 404s
# ("models/gemini-3.7-flash-medium is not found"). These two converters do the respelling; both PARSE agy's fixed
# suffix vocabulary (format, not meaning), so no judgement is involved.
_GEMINI_REASONING_TIERS = ("low", "medium", "high")


def _split_gemini_reasoning(model_id):
    """(bare_id, tier) when a gemini id carries an agy reasoning suffix; (model_id, None) otherwise. For the
    METERED path, which wants the bare id plus a reasoning parameter."""
    for tier in _GEMINI_REASONING_TIERS:
        suf = "-" + tier
        if model_id.endswith(suf) and len(model_id) > len(suf):
            return model_id[: -len(suf)], tier
    return model_id, None


def _compose_gemini_reasoning(model_id, reasoning):
    """The agy-style id that carries `reasoning` as a suffix, for the AGY LANE (which reads effort off the id, not
    a parameter, and ignores the reasoning kwarg). A valid explicit tier (low|medium|high) wins and REPLACES any
    existing suffix; an id that already carries a tier is kept. A BASE id with no requestable tier gets the lane's
    DEFAULT tier — because agy serves ONLY the tier-suffixed forms and REJECTS a bare base id (MEASURED 2026-09-16:
    `--model gemini-3.8-flash` → "invalid model selection"), so returning the bare id (as this once did) MISSES the
    $0 lane and silently falls back to the metered API. The default is the lane's OWN (lane_catalog quirk)."""
    base, existing = _split_gemini_reasoning(model_id)
    tier = str(reasoning or "").strip().lower()
    if tier in _GEMINI_REASONING_TIERS:                  # explicit valid tier wins (replaces any existing suffix)
        return base + "-" + tier
    if existing:                                         # already tier-suffixed → agy serves it, keep it
        return model_id
    return base + "-" + (_reference_data_accessors.resolve_quirk("gemini").get("default") or "medium")


def _is_dated_variant(bare, candidate):
    """True iff `candidate` is exactly `bare` + a DATE suffix `-YYYYMMDD` (8 trailing digits). A FIXED orthographic
    convention (Anthropic dates its ids), so this is PARSING a known shape, never a meaning judgement — an inner
    '-mini-'/'-preview-' segment fails the check, so it can only match the pure dated form of the SAME id."""
    if not candidate.startswith(bare + "-"):
        return False
    suffix = candidate[len(bare) + 1:]
    return len(suffix) == 8 and suffix.isdigit()


def _served_dated_id(provider, model_id):
    """`model_id`, or its SERVED dated variant (`<model_id>-YYYYMMDD`) when the bare id is NOT itself served but a
    dated form IS. The lane's CLI accepts a bare alias (claude-haiku-4-5) that the metered API does not serve — it
    serves only the dated id (claude-haiku-4-5-20251001) — so without this the atomic pair would STRAND a pinned
    claude-haiku call. $0, cache-only: reads catalog.live_model_ids (the synced served list) and matches the fixed
    date-suffix format (_is_dated_variant) — never the agentic closest_served, which is the runtime's confirmed-stale
    hop. Unchanged when the list is absent, the bare id is already served, or no dated variant exists (then
    served_check still flags it 'stale' and the runtime resolver takes over)."""
    try:
        ids = _reference_data_accessors.resolve_live_model_ids(provider)
        if not ids or model_id in ids:
            return model_id                                # no list, or already served → nothing to resolve
        variants = sorted(m for m in ids if _is_dated_variant(model_id, m))
        return variants[-1] if variants else model_id      # newest dated variant, else unchanged (stays flagged stale)
    except Exception:
        return model_id                                    # best-effort — a catalog hiccup never breaks the mapping


def metered_fallback_id(provider, use_name):
    """The metered-API equivalent of a lane USE-NAME. The catalog is the authoring SSOT for the STATIC crosswalk:
    model_catalog.model_record resolves a lane spelling to its base record, whose ``metered_id`` (or own ``id``) is
    authoritative. This reader then layers only runtime facts on top: Gemini's fixed-format reasoning suffix becomes
    a parameter, and the live served-list may upgrade a bare metered alias to its dated form. Unknown compatibility
    ids degrade to the pre-catalog behavior instead of breaking. Returns (metered_id, reasoning_level_or_None)."""
    from ... import model_catalog
    rec = model_catalog.model_record(use_name)
    if rec is None:
        if provider == "gemini":
            return _split_gemini_reasoning(use_name)
        return _served_dated_id(provider, use_name), None
    metered_base = rec.get("metered_id") or rec["id"]
    reasoning = _split_gemini_reasoning(use_name)[1] if provider == "gemini" else None
    return _served_dated_id(provider, metered_base), reasoning


def _cacheable_system(system):
    """True when a system prompt is long enough to be worth prompt-caching. A bound on a REAL quantity (its token
    count vs the provider's cache minimum) — a deterministic threshold, not a meaning judgement. Anthropic and OpenAI
    both need ~1024 tokens for a cache block to form; the shared minimum lives in cacheaudit._MIN_CACHE_TOKENS."""
    if not system:
        return False
    from ... import cacheaudit
    try:
        import tiktoken
        n = len(tiktoken.get_encoding("o200k_base").encode(system))
    except Exception:
        n = len(system) // 4            # a chars→tokens heuristic is adequate for a threshold when tiktoken is absent
    return n >= cacheaudit._MIN_CACHE_TOKENS
