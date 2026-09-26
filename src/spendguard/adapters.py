"""Provider adapters for the `compare` harness.

Most providers expose an OpenAI-compatible API, so adding one is a single registry entry
(name, base_url, key env, model prefixes). Anthropic uses its own SDK. Add more at runtime
with register_provider(...). Calls go through the openai/anthropic SDKs, so the spend gate
already meters + budgets them.
"""
import time
import json
import threading
from . import config, pricing

# name -> {base_url, key_env, prefixes, kind}
PROVIDERS = {
    "openai":    {"base_url": None, "key_env": "OPENAI_API_KEY",
                  "prefixes": ("gpt-", "o1", "o3", "chatgpt"), "kind": "openai", "batch": True},
    "anthropic": {"base_url": None, "key_env": "ANTHROPIC_API_KEY",
                  "prefixes": ("claude-",), "kind": "anthropic"},
    "gemini":    {"base_url": "https://generativelanguage.googleapis.com/v1beta/openai/",
                  "key_env": "GEMINI_API_KEY", "prefixes": ("gemini-",), "kind": "openai"},
    "deepseek":  {"base_url": "https://api.deepseek.com",
                  "key_env": "DEEPSEEK_API_KEY", "prefixes": ("deepseek",), "kind": "openai"},
    "qwen":      {"base_url": "https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
                  "key_env": "DASHSCOPE_API_KEY", "prefixes": ("qwen", "qwq"), "kind": "openai"},
    "zai":       {"base_url": "https://api.z.ai/api/paas/v4",   # z.ai / Zhipu GLM — OpenAI-compatible (verify base_url with your key)
                  "key_env": "ZAI_API_KEY", "prefixes": ("glm-",), "kind": "openai"},
    "voyage":    {"base_url": "https://api.voyageai.com/v1",     # Voyage AI — OpenAI-compatible /EMBEDDINGS only (no
                  #                                                chat, no async Batch API). Anthropic's recommended
                  #                                                embeddings; reached via adapters.embed(model="voyage-…").
                  "key_env": "VOYAGE_API_KEY", "prefixes": ("voyage-",), "kind": "openai", "batch": False},
    "moonshot":  {"base_url": "https://api.moonshot.ai/v1",     # Moonshot AI (Kimi) — OpenAI-compatible.
                  # Prefixes cover the WHOLE family (kimi-k2/k2.5/k2.6/kimi-latest and any future kimi-*), so a
                  # newer Kimi routes + prices itself the day the synced table carries it — no code change, no
                  # hardcoded rate. Mainland-China accounts use api.moonshot.cn: register_provider() to override.
                  "key_env": "MOONSHOT_API_KEY", "prefixes": ("kimi", "moonshot-"), "kind": "openai"},
}


def register_provider(name, base_url, key_env, prefixes, kind="openai"):
    """Add/override a provider. kind: 'openai' (OpenAI-compatible) or 'anthropic'."""
    PROVIDERS[name] = {"base_url": base_url, "key_env": key_env, "prefixes": tuple(prefixes), "kind": kind}


def provider_for(model):
    """Resolve provider from a model id. Accepts explicit 'provider:model' too. When several providers' prefixes
    match, the LONGEST (most specific) prefix wins — DETERMINISTIC, independent of PROVIDERS insertion order, so a
    plugin whose prefix overlaps a built-in can't resolve by dict order and silently bill the wrong vendor."""
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
        from . import config as _cfg
        m = (_cfg._cfg_get("advisor", "provider_base_model", {}) or {}).get(prov)
        if m:
            return m
    except Exception:
        pass
    try:
        from . import model_catalog as _mc
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
    from . import model_catalog as _mc
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


def _executor():
    v = __import__("os").environ.get("SPENDGUARD_ADVISOR_EXECUTOR")
    if v:
        return v.strip().lower()
    try:
        return str(config._cfg_get("advisor", "executor", "api")).lower()
    except Exception:
        return "api"


# ── subscription lanes (executor=pool uses both; each serves ONLY its own provider's prompts) ──
# A lane failure (window exhausted, CLI missing, parse mismatch) puts THAT lane on an in-process
# cooldown so a burst of meta prompts doesn't hammer a dead lane — during cooldown calls go straight
# to the caged API. Provider-respecting on purpose: a claude-model prompt never silently runs on the
# ChatGPT plan (or vice versa) — the recorded model must be the model that answered.
from . import lane_registry
_LANES = lane_registry.provider_lane_map()   # {provider: (lane, exec_module_name)} — DERIVED from the ONE lane
#                                              registry (add a lane THERE, not here); the name is imported in _lane_for.
from . import resource_state   # AXIS-1 of the resource_state migration: cooldowns now live in the unified store
_sub_guard = threading.local()   # one-hop lane-substitution guard: while a substitute call is in flight (proactive OR
                                 # reactive), no nested substitution — a substitute failing does not chain to a third.
_resolve_guard = threading.local()  # while the agentic model-RESOLVER's own advisor call is in flight, dispatch does
                                    # not re-resolve — the resolver picks an id, it must not be resolved again (recursion).
_heal_guard = threading.local()     # while discover_efforts is PROBING which reasoning tiers an endpoint accepts, the
                                    # rung must DROP a rejected effort (so discovery sees it as rejected), NOT heal it —
                                    # else the probe self-heals, looks accepted, and discovery learns the opposite.
_route_guard = threading.local()    # while a call is inside a routed durable RECORD (advisor.route_through_queue), a
                                    # nested call (a substitution / fallback) is NOT re-recorded — one durable row per
                                    # logical call, no double-record and no loop.
_lane_echoed = set()             # lanes already announced this run — echo "a plan is serving these prompts" ONCE per
                                 # lane so the user KNOWS their work rode a subscription (not once per call = spam).


def _pool_cooldown_s():
    import os as _os
    try:
        return float(_os.environ.get("SPENDGUARD_POOL_COOLDOWN_S")
                     or config._cfg_get("advisor", "pool_cooldown_s", 900))
    except Exception:
        return 900.0


def _max_quota_cool_s():
    """Cap on how long a QUOTA/reset-window failure cools a lane. A provider may STATE a huge window ("resets in
    95h"), but a plan whose quota OSCILLATES (agy's Starter quota serves some calls even while reporting a 95h
    reset) must be RE-TESTED periodically, not bypassed for days — the first call after this cap IS the re-test,
    and a success clears the cool. Config advisor.max_quota_cool_s (default 1800s = 30m); env override."""
    import os as _os
    try:
        return float(_os.environ.get("SPENDGUARD_MAX_QUOTA_COOL_S")
                     or config._cfg_get("advisor", "max_quota_cool_s", 1800))
    except Exception:
        return 1800.0


def _lane_cooling(lane):
    return resource_state.cooling(resource_state.lane_key(lane))


def _lane_cool(lane, seconds=None, reason=""):
    """Cool a whole lane for `seconds` (default the pool cooldown), tagged with `reason` (quota / down / failover).
    A quota exhaustion passes its (capped) reset window so the lane stays down until it resets, instead of being
    retried every 900s only to re-fail. Delegates to the unified resource_state store — which persists it so a
    FRESH bulk process honours a still-active cool (a quota exhaustion must outlive the process that hit it)."""
    resource_state.cool(resource_state.lane_key(lane), float(seconds) if seconds else _pool_cooldown_s(), reason)
    # EVENT-DRIVEN health: only a 'down' (the lane AND its metered-API fallback BOTH failed to serve this call) is a
    # candidate for a "this lane needs you" alert — and reliability CONFIRMS it with a bounded probe before alarming,
    # so a TRANSIENT blip under load (that recovers on the next call) never toasts. A 'failover' is deliberately NOT
    # alarmed: a substitute lane CARRIED the work, so the call SUCCEEDED gracefully — the brief backoff cool above is
    # all that's warranted; toasting "lane failing" when the work just succeeded via fallback is a false alarm (the
    # exact flapping the user hit under load). 'quota'/model-miss are transient too (the cool + reset handle them).
    # The reason codes are the ones THIS function was passed (mechanical), never a judgement about an error. Fail-safe.
    if reason == "down":
        try:
            from . import reliability
            reliability.note_lane_down(lane, reason)
        except Exception:
            pass


import threading as _threading
import time as _time_surface
# A throttle MEMO with staleness (not a permanent suppressor): {lane: monotonic ts of the last surfaced down}. A
# $0-lane batch is told ONCE per lane per window rather than once per failed task, but a LATER outage — the lane
# recovered then failed again after the window — DOES re-surface (the entry is stale). Window matches
# reliability._EVENT_NOTIFY_THROTTLE_S so the inline line and the macOS notify agree on cadence.
_LANE_DOWN_SURFACED_AT = {}
_LANE_DOWN_LOCK = _threading.Lock()
_LANE_DOWN_THROTTLE_S = 1800.0


def _surface_lane_down(lane, err):
    """Tell the user (once per lane per _LANE_DOWN_THROTTLE_S) that a lane could not serve — so a $0-lane batch never
    fails SILENTLY (the empty-and-skipped the user hit). Names the exact re-login step (a lane_registry lookup) and
    points at the agentic diagnosis for the specific cause. Best-effort, never raises — this is on the call path."""
    try:
        now = _time_surface.monotonic()
        with _LANE_DOWN_LOCK:
            last = _LANE_DOWN_SURFACED_AT.get(lane)
            if last is not None and (now - last) < _LANE_DOWN_THROTTLE_S:
                return                                   # already surfaced within the window; a stale entry re-surfaces
            _LANE_DOWN_SURFACED_AT[lane] = now
        import sys as _sysd
        from . import reliability
        hint = reliability.lane_login_hint(lane)
        msg = (f"[spendguard] {lane} lane could not serve (the executor errored): {str(err)[:80]} — cooled and "
               f"rerouting (other $0 lanes first, then metered) so work is not lost.")
        if hint:
            msg += f" If it is a login/token expiry, re-activate: {hint}."
        msg += " Run `spendguard reliability --run --remediate` for the exact fix."
        print(msg, file=_sysd.stderr)
    except Exception:
        pass


def _lane_model_cooling(lane, model):
    return resource_state.cooling(resource_state.lane_model_key(lane, model))


def _lane_model_cool(lane, model):
    """Back off ONE (lane, model) after the lane failed for it WITHIN a proven-good size and the API then answered
    — a MODEL/content mismatch (e.g. codex 400s gpt-5-mini on a ChatGPT plan: "not supported with a ChatGPT
    account"), not the lane being down. Per-model so gpt-5.5 keeps riding codex the whole time; self-healing (it
    expires) so a model the plan later serves is retried. Without this the lane re-intercepts the rejected model on
    EVERY call — the '_learn_from_fallback' size axis alone can't see a model rejection. Its own cooldown axis in
    the resource_state store (a distinct key from the whole-lane cool), tagged 'model-miss'."""
    resource_state.cool(resource_state.lane_model_key(lane, model), _pool_cooldown_s(), "model-miss")


# A lane failure is NOT always the lane being down. A CLI that ran the prompt as an AGENT and hit its turn limit,
# or a prompt that overran the plan model's context, is a PROMPT-vs-lane MISMATCH — the lane is fine for other
# prompts — so cooling the WHOLE lane for 900s (metering even small prompts after it) is the wrong response.
# Measured on a 4-LLM code review: one big file's claude-code result came back error_max_turns and cooled the
# lane. Rather than parse each lane's error TEXT to guess which kind it was (fragile, and every lane words it
# differently), the SYSTEM uses the fact it already has: it falls back to the API anyway, so the API OUTCOME on
# the SAME prompt settles it — the API answered where the lane did not ⇒ the lane was UNSUITABLE for this prompt
# (keep it, and route prompts this size straight to API); the API failed too ⇒ a real problem (cool the lane).
# One signal, every lane, and no interpretation of an error string.
def _lane_note_ok(lane, prompt):
    """Record that the lane ANSWERED a prompt of this size — the proven-good watermark below which an 'unsuitable'
    failure is content-specific, not a size limit, so a small anomaly can never disable a lane that demonstrably
    handles that size. Lives in the resource_state store's size_ceiling axis (persistent, survives processes)."""
    resource_state.note_proven_good(resource_state.lane_key(lane), len(prompt or ""))
    try:                                             # a SERVED call RESOLVES any event-down for this lane — the recovery
        from . import reliability                     # signal that clears the health row + makes the next down a fresh
        reliability.note_lane_ok(lane)               # blip (not read as a sustained outage). Best-effort; never blocks a call.
    except Exception:
        pass


def _lane_too_big(lane, prompt):
    """True when a prompt this size already provoked a GENUINE size failure on this lane (above its proven-good
    size) — route it straight to the API rather than pay the lane's cold start to fail again. The learned ceiling
    lives in the resource_state store, persisted with a RE-TEST window, so a one-off failure never bypasses the
    lane forever and a real limit is not re-learned from scratch each process (the old in-memory ceiling was)."""
    ceil = resource_state.size_ceiling(resource_state.lane_key(lane))
    return ceil is not None and len(prompt or "") >= ceil


def _learn_from_fallback(lane_name, prompt, api_failed, model=None, transient=False):
    """The auto-route decision from the API-fallback OUTCOME (never the lane's error string). The REASON for the
    miss decides what is learned — throwing the reason away is exactly what let a quota miss disable a lane:
      api_failed=True  → the API ALSO failed ⇒ a real problem: cool the lane. Returns "down".
      transient=True   → the lane failed with a STRUCTURED quota/rate signal (a parsed reset window) ⇒ NOT a size
                         or content limit. The lane was already cooled UNTIL its reset window, so learn NOTHING
                         about suitability and never pin a size ceiling. Returns "transient". (THE FIX: a quota
                         miss used to be indistinguishable from "prompt too big" and permanently bypassed the lane.)
      otherwise the API answered where the lane did not ⇒ unsuitable for THIS prompt. A SIZE ceiling is learned
      ONLY when the failing size is LARGER than a size the lane has PROVEN it can answer (proven_good > 0) — a
      genuine size signal. Without a proven-good baseline the miss is ambiguous (schema/content/a not-yet-parsed
      transient), so back off THIS model on THIS lane (retryable) rather than pin a PERMANENT size ceiling from the
      very first miss — the poison that starved a whole working lane. The signals are FACTS (did the API answer? a
      structured quota token?), never a keyword read of the prose."""
    if api_failed:
        _lane_cool(lane_name, reason="down")
        return "down"
    if transient:
        return "transient"
    n = len(prompt or "")
    _ok = resource_state.proven_good(resource_state.lane_key(lane_name))
    if _ok > 0 and n > _ok:                         # failure ABOVE a proven-good size ⇒ a genuine size limit
        resource_state.set_size_ceiling(resource_state.lane_key(lane_name), n)   # min-ratchet + re-test window
        return "unsuitable"
    if model:                                       # no proven-good baseline (or within it) ⇒ model/content miss,
        _lane_model_cool(lane_name, model)          # retryable — back off THIS model on THIS lane, NOT a permanent
    return "model-cooled"                           # size ceiling from an ambiguous first miss


def _lane_for(prov):
    """(lane_name, exec_module) if the configured executor covers this provider's prompts, else None.
    `pool` enables every provider's lane; a single-lane setting enables only its own provider."""
    ex = _executor()
    lane, mod = _LANES.get(prov, (None, None))
    if lane is None or ex not in ("pool", lane):
        return None
    if _lane_cooling(lane):
        return None
    import importlib
    try:
        return lane, importlib.import_module(f".{mod}", __package__)
    except Exception:
        return None


class SchemaNotStrictExpressible(ValueError):
    """A JSON Schema OpenAI strict mode cannot express without SILENTLY emptying it — surfaced as a loud, typed
    error instead of the {} it would otherwise return. Carries `.path` (where the offending map is)."""

    def __init__(self, message, path=None):
        super().__init__(message)
        self.path = path


# OpenAI structured-outputs (strict) limit on the TOTAL number of enum values across one schema. Not a preference:
# it is the provider's own contract, quoted from its 400 — "Expected at most 1000 enum values in total within a
# single schema when using structured outputs". Named here because it is checked BEFORE a request is built (see
# json_schema_request): the realtime path can heal a rejected response_format by re-sending unenforced, but a BATCH
# cannot heal per row, so a schema over this limit fails EVERY row of every batch built from it — measured on a
# consumer: 12 of 12 batches, 0 completed, 100% `invalid_request_error`, for weeks, read downstream as "empty".
OPENAI_STRICT_MAX_ENUM_VALUES = 1000


def strict_enum_total(schema):
    """The number of enum values OpenAI strict mode counts for `schema` — every `enum` OCCURRENCE summed, because
    that is how the provider counts (four fields sharing one 256-value list were billed as 1024, not 256). A
    mechanical walk over a fixed structure (properties / items / anyOf / oneOf / allOf), never a judgement."""
    if not isinstance(schema, dict):
        return 0
    n = len(schema["enum"]) if isinstance(schema.get("enum"), list) else 0
    for v in (schema.get("properties") or {}).values():
        n += strict_enum_total(v)
    if isinstance(schema.get("items"), dict):
        n += strict_enum_total(schema["items"])
    for comb in ("anyOf", "oneOf", "allOf"):
        for v in (schema.get(comb) or []):
            n += strict_enum_total(v)
    return n


def strict_map_violation(schema, _path="$"):
    """The first path where an object uses a DYNAMIC-KEY MAP (`additionalProperties` set to a sub-SCHEMA, i.e. a
    dict) — or None if the schema is strict-expressible. OpenAI strict mode forces additionalProperties=false and
    `required`=every DECLARED property (_openai_strict); a map declares none, so strict collapses it to the only
    value left: {}. The model then 'correctly' returns an empty object and no error fires. This is PARSING the
    schema's SHAPE (additionalProperties being a dict is a fixed structural fact), never judging meaning — so a
    literal shape check is right here, not an LLM. `additionalProperties: false`/absent is fine (not a map)."""
    if not isinstance(schema, dict):
        return None
    if schema.get("type") == "object" and isinstance(schema.get("additionalProperties"), dict):
        return _path
    for k, v in (schema.get("properties") or {}).items():
        hit = strict_map_violation(v, "%s.%s" % (_path, k))
        if hit:
            return hit
    items = schema.get("items")
    if isinstance(items, dict):
        hit = strict_map_violation(items, "%s[]" % _path)
        if hit:
            return hit
    return None


def json_schema_request(kind, schema, name="result"):
    """Per-vendor kwargs that make the VENDOR enforce the shape, instead of a prompt asking politely.

      anthropic → a forced tool call: the model must emit tool_use conforming to input_schema
      openai    → response_format json_schema with strict=True
      others (OpenAI-compatible: GLM, Kimi, DeepSeek…) → response_format json_object, which guarantees
                  parseable JSON but NOT the shape — so the local validator still has to run. Claiming
                  otherwise would be assuming a capability we have not measured on those endpoints.

    Enforcement guarantees the KEY exists. It does NOT guarantee the value means anything — `line_start: 0`
    satisfies every strict schema ever written. That is what `nonempty` in output_contract is for.

    TWO DIALECTS THAT LOOK IDENTICAL. output_contract's schema is OURS: it carries `nonempty`, which is not
    JSON Schema and which no provider has ever heard of. Handing it to a provider verbatim is the same class
    of error as reading a context window as an output ceiling — two things of the same shape, meaning
    different things. Measured: gpt-5.5 rejected the contract outright (400, "'additionalProperties' is
    required to be supplied and to be false"), while kimi-k3 and glm-5.2 accepted the request and invented a
    shape, because the compat path told them "return JSON" and never told them WHICH json."""
    if not isinstance(schema, dict):
        return {}
    if kind == "anthropic":
        return {"tools": [{"name": name, "description": "Return the result in this exact shape.",
                           "input_schema": _provider_schema(schema)},
                          ],
                "tool_choice": {"type": "tool", "name": name}}
    if kind == "openai":
        _v = strict_map_violation(schema)
        if _v:
            raise SchemaNotStrictExpressible(
                "schema at %s is a dynamic-key MAP (additionalProperties: <schema>). OpenAI strict mode forces "
                "additionalProperties=false + required=every declared property, so a map has no allowed keys and "
                "the model returns {} with NO error. Restructure it as an ARRAY of {key,value} objects "
                "(e.g. results:[{id,label}], not labels:{<id>:<label>}) — reliable on every vendor." % _v, path=_v)
        # THE ENUM LIMIT IS CHECKED HERE, ONCE, FOR EVERY PATH. A map schema is refused above because strict mode
        # would silently EMPTY it; an over-limit enum is refused because strict mode REJECTS it outright — and on
        # a batch there is no per-row heal, so every request in the file 400s and the caller reads a file of
        # failures as a file of empties. Refusing at build costs nothing; the alternative cost a consumer weeks.
        _n_enum = strict_enum_total(schema)
        if _n_enum > OPENAI_STRICT_MAX_ENUM_VALUES:
            raise SchemaNotStrictExpressible(
                "schema declares %d enum values in total; OpenAI strict mode accepts at most %d per schema and "
                "returns 400 for the whole request otherwise (a batch cannot heal this per row — every line fails). "
                "Shrink the enum (an id space sized to what is actually shown), share one smaller list, or use a "
                "plain string validated after the call." % (_n_enum, OPENAI_STRICT_MAX_ENUM_VALUES))
        return {"response_format": {"type": "json_schema",
                                    "json_schema": {"name": name, "schema": _openai_strict(schema),
                                                    "strict": True}}}
    # OpenAI-COMPATIBLE, not OpenAI. json_object guarantees parseable JSON and nothing about the shape, so
    # the shape has to travel in the PROMPT — the caller gets it appended rather than being left to hope.
    return {"response_format": {"type": "json_object"},
            "_schema_prompt": "Return ONLY a JSON object conforming exactly to this JSON Schema. No prose, "
                              "no code fence, no explanation.\n" + json.dumps(_provider_schema(schema))}


# THE provider → JSON-schema DIALECT, in ONE place. json_schema_request TAKES the dialect; this DECIDES it from the
# provider, so the realtime call sites (anthropic @ _call_once and the openai/compat one) and the capability router
# below can never disagree about which vendor enforces a shape. anthropic = forced tool, openai = strict
# response_format (both ENFORCE the shape); every other OpenAI-compatible vendor (zai/moonshot/deepseek/gemini) =
# json_object (parseable JSON only — the shape is validated locally by output_contract, not enforced by the vendor).
def _schema_kind(provider):
    if provider == "anthropic":
        return "anthropic"
    if provider == "openai":
        return "openai"
    return "compat"


SCHEMA_STRICT = "strict"              # the vendor ENFORCES the shape (anthropic forced-tool / openai strict)
SCHEMA_JSON_OBJECT = "json_object"    # JSON is guaranteed, the SHAPE is not — output_contract validates it


def schema_capability(provider):
    """How strongly `provider`'s METERED path can hold output to a JSON schema: SCHEMA_STRICT (anthropic/openai — the
    vendor enforces the exact shape) or SCHEMA_JSON_OBJECT (every other OpenAI-compatible vendor — JSON guaranteed, the
    shape is not). Derived from _schema_kind so it cannot disagree with the request json_schema_request actually builds.
    A LANE is prompt-only regardless (a CLI completion carries no response_format/tool param), so a lane NEVER has
    strict capability even for a strict-capable vendor — which is exactly why a strict schema on a prompt-only lane
    must route to that vendor's metered path (the capability-aware auto-route)."""
    return SCHEMA_STRICT if _schema_kind(provider) in ("anthropic", "openai") else SCHEMA_JSON_OBJECT


def _provider_schema(schema):
    """Our contract, stripped to what a provider can actually parse. `nonempty` is a spendguard concept and
    stays local — it is checked against the RESPONSE, never sent as if it were JSON Schema."""
    if not isinstance(schema, dict):
        return schema
    out = {}
    for k, v in schema.items():
        if k == "nonempty":
            continue
        if isinstance(v, dict):
            out[k] = _provider_schema(v)
        elif isinstance(v, list):
            out[k] = [_provider_schema(x) if isinstance(x, dict) else x for x in v]
        else:
            out[k] = v
    return out


def _openai_strict(schema):
    """OpenAI strict mode has two rules beyond JSON Schema, and violating either is a 400, not a soft failure:
    every object must set additionalProperties=false, and `required` must list EVERY property (optionality is
    expressed with a null type, not by omission). Applied recursively so nested items comply too."""
    s = _provider_schema(schema)
    if not isinstance(s, dict):
        return s
    if s.get("type") == "object":
        props = s.get("properties") or {}
        s = dict(s, additionalProperties=False,
                 properties={k: _openai_strict(v) for k, v in props.items()},
                 required=list(props.keys()))
    elif s.get("type") == "array" and isinstance(s.get("items"), dict):
        s = dict(s, items=_openai_strict(s["items"]))
    return s


# DEFAULT: no cap. `max_tokens=512` sat here (now REMOVED) and quietly capped every caller who did not name a
# number — including callers that deliberately pass none precisely so the measured budget applies. _call_guarded
# resolves None on the OUTPUT axis: the 32k floor (TOKEN_FLOOR), RAISED — never lowered — by the call-class's own
# measured history (autotune `recommend`). That is the mechanism this package built for exactly this, and which a
# default of 512 skipped entirely. (None of this concerns the INPUT: input is bounded separately by the window.)
#
# A cap was never cost control: you are billed for the tokens GENERATED, so a low cap saves nothing and
# instead truncates the answer — and a truncated JSON body reads downstream as "no findings" rather than
# "no answer". That is the whole recurring failure. The number now comes from measurement or from the
# caller, never from a literal nobody chose.
def _book_substitution(r):
    """Book a SUBSTITUTION as ONE record that serves BOTH pillars — the value proof AND the learner's evidence.

    The baseline is r['substituted_from'] (the model the caller WOULD have run), priced at the tokens ACTUALLY
    used; the chosen model is what ran. A per-decision row (intent, requested→chosen model+effort, counterfactual $,
    actual $, saved $) is recorded for EVERY substitution — best-value OR the utilisation bandit. A metered→cheaper
    swap also books a guarded saving on the THIRD axis; a $0 plan-lane swap books NO saving and saved_usd stays 0
    (that avoided $ is the EST-VALUE axis — guard.record_saving refuses 'plan'/double-counting), but the decision
    is still recorded with counterfactual_usd so the plan-served value is visible. Best-effort; never raises."""
    try:
        if not isinstance(r, dict) or not r.get("substituted_from"):
            return
        requested = r.get("substituted_from")            # the model the caller would have run (the honest baseline)
        cost = r.get("cost")
        if cost is None:                                 # errored/refused → nothing ran, nothing to book
            return
        from . import pricing, guard, calls as _cbk
        actual = float(cost or 0.0)
        try:
            base = float(pricing.realtime_cost(requested, int(r.get("in_tok") or 0), int(r.get("out_tok") or 0)) or 0.0)
        except Exception:
            base = 0.0                                   # unpriced baseline → no counterfactual $ to credit
        # saved_usd counts ONLY a metered→cheaper-metered swap — the same amount that goes to the savings ledger,
        # so Σ decisions.saved_usd == the savings tally. A $0 plan swap's avoided $ is est-value, not a saving here.
        saved = (base - actual) if (actual > 0 and base > actual) else 0.0
        _basis = "best-value" if r.get("best_value") else "advisor"
        chosen = f"{r.get('provider')}:{r.get('model')}" if r.get("provider") else r.get("model")
        guard.record_decision(intent=(_cbk.current() or {}).get("intent"),
                              requested_model=requested, requested_effort=r.get("requested_effort"),
                              chosen_model=chosen, chosen_effort=r.get("chosen_effort"),
                              counterfactual_usd=base, actual_usd=actual, saved_usd=saved, basis=_basis,
                              why=r.get("substitution"))   # the SPECIFIC routing reason (bandit/lane/tier-3/best-value)
        if saved > 0:                                    # metered → cheaper metered: a real counterfactual saving
            guard.record_saving(_basis, saved)
    except Exception:
        pass


# THE IN-PROCESS WARN-ONCE LEDGER for silent model swaps — the deliberate twin of pricing.UNPRICED_SEEN (a
# per-process notice registry, not a cache). MONOTONIC BY DESIGN: a (requested → served) pair, once announced,
# stays announced for the life of the process, so a bulk loop that swaps the same pair 1,000× prints ONE line,
# not a thousand. There is no staleness to handle — an entry never goes out of date (the pair either has been
# announced this process or has not) and is never cleared; it is bounded by the tiny number of DISTINCT swap
# pairs seen (a handful of models). `_warn_once_if_substituted` is its only writer.
_SUBST_WARN_LEDGER = set()
# Same once-only, process-lifetime contract + bound (a handful of distinct call-classes) as _SUBST_WARN_LEDGER:
# `_warn_once_caller_maxtokens` is its only writer. Staleness is harmless (worst case a duplicate advisory line).
_MAXTOK_WARN_LEDGER = set()


def _warn_once_caller_maxtokens(key, passed):
    """Announce — LOUD, once per call-class — that a caller passed an explicit max_tokens that spendguard IGNORED.
    spendguard OWNS the output budget (output_budget): max_tokens is a CEILING billed on ACTUAL tokens, so the budget is
    the model's real ceiling (or the 32K floor) — a caller value can only ever set it too LOW and truncate a real answer.
    The fix is to STOP passing it. Returns the message on the first announcement (for tests), else None.

    Dedup is the module-level _MAXTOK_WARN_LEDGER memo (process-lifetime, bounded to distinct call-classes): its staleness
    is harmless by construction — the worst case of a lost entry is one repeated advisory line, never a wrong result."""
    if key in _MAXTOK_WARN_LEDGER:
        return None
    _MAXTOK_WARN_LEDGER.add(key)
    msg = ("[spendguard] max_tokens=%d passed (%s) — IGNORED. spendguard owns the output budget: it sends the model's "
           "real ceiling (billed on ACTUAL tokens, so a high ceiling is free); a caller cap can only truncate. Omit it."
           % (passed, key))
    import sys as _sysmt
    print(msg, file=_sysmt.stderr)
    return msg


def _warn_once_if_substituted(r):
    """Announce — LOUD, and exactly ONCE per (requested → served) pair — that an EXPLICITLY-NAMED model was
    silently swapped by a lane/bandit substitution. A best-value delegation is intentional and prints its own
    notice, so it is exempted below.

    A caller who NAMED a model and gets a different one recorded (`substituted_from`) is fine for ordinary
    utilisation routing, but SILENTLY WRONG for a MEASUREMENT — a bakeoff, a cross-vendor panel, any call where
    WHICH MODEL ANSWERED is the result (the 2026-08-29 'consensus panel was one model' class). The swap was
    already recorded in `substituted_from`, but a measurement caller does not read that field; this makes it
    VISIBLE and names the fix.

    Returning None on a REPEAT of the same pair is the intended once-only contract (see the ledger note above),
    not a dropped warning. Returns the message on the FIRST announcement (for tests), else None. Pin the vendor
    with no_substitution=True."""
    if not isinstance(r, dict):
        return None
    sub_from, served = r.get("substituted_from"), r.get("model")
    if not sub_from or r.get("best_value") or sub_from == served:
        return None                                      # no swap · best-value (intentional, already noted) · self
    pair = (sub_from, served)
    if pair in _SUBST_WARN_LEDGER:                        # already announced this process → once-only, by contract
        return None
    _SUBST_WARN_LEDGER.add(pair)
    msg = (f"[spendguard] you named {sub_from!r} but it was served by {served!r} (lane/bandit substitution). If "
           f"the MODEL is the measurement (bakeoff / cross-vendor panel), pass no_substitution=True to pin it.")
    import sys as _syssub
    print(msg, file=_syssub.stderr)
    return msg


def _default_reasoning_is_best_value():
    """Is the config-scoped best-value DEFAULT armed? Env SPENDGUARD_DEFAULT_REASONING wins, else config
    advisor.default_reasoning. Default OFF (unset). Mirrors the env-then-config pattern used across the package."""
    import os
    v = os.getenv("SPENDGUARD_DEFAULT_REASONING")
    if v is not None:
        return v.strip().lower() == "best-value"
    from . import config
    return str(config._cfg_get("advisor", "default_reasoning", "") or "").strip().lower() == "best-value"


def _route_through_queue_enabled():
    """Is 'route every labelled synchronous call through the durable lane_queue' armed? Env
    SPENDGUARD_ROUTE_THROUGH_QUEUE wins, else config advisor.route_through_queue. ON by default now that the queue
    write is POOLED (~46us/call, measured — was ~950us/op per-op) so recording every labelled call is cheap: a leased
    row gives observability + crash-recovery + priority/SLA metadata, and a non-deliberate record failure runs the
    call UNRECORDED (never gates the result). Opt OUT with advisor.route_through_queue=false or
    SPENDGUARD_ROUTE_THROUGH_QUEUE=0."""
    import os
    v = os.getenv("SPENDGUARD_ROUTE_THROUGH_QUEUE")
    if v is not None:
        return v.strip().lower() in ("1", "true", "yes", "on")
    from . import config
    return bool(config._cfg_get("advisor", "route_through_queue", True))


def _manage_all_enabled():
    """Is UNIVERSAL ADMISSION armed — should a plain labelled adapters.call be PACED through the dispatch governor
    (concurrency + RPM + TPM), not just recorded? This is what makes 'with a managed queue, no user ever sees a 429'
    real: without it, only governed=True / bulk_delegate fans are paced, so a serial caller (or a consumer's own thread
    pool) blows a provider's tokens/minute ceiling. ON by default (the governor is cheap when uncontended, and TPM/RPM
    pace only once a real per-vendor limit is configured or learned). Env SPENDGUARD_DISPATCH_MANAGE_ALL wins, else
    config dispatch.manage_all. Kill switch SPENDGUARD_DISPATCH_OFF=1 disables ALL admission underneath this."""
    import os
    v = os.getenv("SPENDGUARD_DISPATCH_MANAGE_ALL")
    if v is not None:
        return v.strip().lower() in ("1", "true", "yes", "on")
    from . import config
    return bool(config._cfg_get("dispatch", "manage_all", True))


def _apply_best_value_default(reasoning, intent, sig, no_substitution, probe):
    """Config-scoped default (opt-in; OFF unless advisor.default_reasoning='best-value'): turn reasoning=None into
    'best-value' for a LABELLED (intent/sig), UNPINNED, non-probe call — so delegated work routes to the cheapest
    (model, effort) whose MEASURED quality holds, without every caller opting in. Returns reasoning UNCHANGED in
    every other case: an explicit reasoning (the caller always wins), a pinned (no_substitution) call, a probe, an
    UNLABELLED call (no intent/sig — the default never triggers prompt-inference), or the default disabled. Pure +
    side-effect-free so the policy is unit-tested without a live call; best_value still degrades to the caller's own
    model when there is no measured evidence, so this can only ever pick a measured-equal-or-better arm."""
    if reasoning is not None or no_substitution or probe or not (intent or sig):
        return reasoning
    return "best-value" if _default_reasoning_is_best_value() else reasoning


def call(model, prompt, max_tokens=None, system=None, reasoning=None, schema=None, timeout_s=None,
         sig=None, intent=None, retries=2, files=None, _no_guard=False, no_metered_fallback=False, images=None,
         no_substitution=False, metered_only=False, base_fallback=None, _probe=False, _route=True, **aliases):
    """Run one prompt against one model. Returns a result dict (never raises).

    `governed=True` (a kwarg carried via **aliases) runs THIS call inside the dispatch GOVERNOR — for a caller
    fanning many concurrent calls (a bakeoff / review panel). It bounds per-lane/vendor in-flight, and on a
    saturated $0 lane SHEDS to that lane's metered twin instead of a per-call metered spill (the shared
    dispatch.admit brain, identical to vendor_call.call); a saturated metered vendor (or no_metered_fallback)
    returns a TYPED deadline dict. `timeout_s` is the queue-wait bound (derived from measured latency if omitted).
    Same dict return, same no_substitution/intent/metered_only semantics. A SINGLE call omits governed (no overhead).

    `metered_only=True` FORCES the paid metered API and SKIPS the subscription lane entirely (the opt-in metered
    HALF of the atomic lane→metered pair). Use it for a consistency-sensitive fan (a verdict-cached refuter) that
    needs the concurrency-invariant metered path serial-equivalently — where riding the $0 lane CLI (its own
    reasoning scale + warm-daemon concurrency) would drift the verdict. Default False = the lane serves first and a
    miss falls back to the metered API (the cheap, faithful-for-capability default). A vision call (`images=`) is
    metered by construction regardless.

    `files=[path, …]` is the INPUT twin of the output guard below: each path is assembled into the prompt as a
    WHOLE, stamped, self-verified block by llm_files.attach_many (raises rather than send a partial file). The
    caller hands PATHS, so it has no way to pre-truncate a file into the prompt — the same discipline the output
    side applies to never reading a truncated reply as a short answer.

    THE TOKEN CONTROLS LIVE HERE, IN THE ONE FUNCTION EVERY CALL ALREADY GOES THROUGH. They were built
    twice over and wired nowhere: bulkgate.is_truncated ("a fact, not a guess") had no caller outside its
    module, bulkgate.maxtokens learned the p99 output length per call-class and no judging script used it,
    and vendor_call.input_limit had exactly one caller — a probe script. So every caller hand-picked a
    number and then read the reply as if the number had been right.

    The first attempt at fixing this put the logic in a SIBLING function (call_complete) and left this one
    free to return a cut body. That is opt-in safety, which is the same failure one level up: an hour after
    it shipped, 1 of 9 judging scripts used it, and a name-review re-run came back with 40 of 79 groups
    "UNREVIEWED" that were really just truncated. A control the caller must remember to use is a control
    that will not be there when it matters, so both now happen unconditionally, here.

    INPUT AND OUTPUT ARE INDEPENDENT — bounded by DIFFERENT model numbers, neither constraining the other:
      INPUT   the prompt is measured against the model's INPUT window (pricing.max_input_tokens) BEFORE sending.
              Over it → an explicit error, not a request the vendor silently clips. This never touches max_tokens.
      OUTPUT  the reply budget floors at TOKEN_FLOOR and is clamped by the model's OUTPUT ceiling
              (pricing.max_output) — set from the OUTPUT axis alone, never lowered by how large the input was.
              A reply the provider marks as cut off is RETRIED at double the budget; if it still will not fit,
              text=None and truncated=True, so a truncated body can never be read as a short answer.

    `sig` names the call-class so the OUTPUT budget comes from its measured p99 (never a literal nobody picked),
    and so each reply feeds that measurement. `intent` is the caller-facing ALIAS for it (the job-type label
    advise/best-value/attribution key on): pass either — most callers pass one; if both, `sig` is the finer
    call-class and `intent` the job-type. `reasoning` (minimal|low|medium|high) sets reasoning effort for
    gpt-5/o-series models; defaults to 'minimal' for them (default-medium reasoning eats the token budget →
    empty output, and costs more — wrong for simple classify/extract calls).
    `reasoning="best-value"` DELEGATES the choice: spendguard resolves the cheapest (model, effort) whose MEASURED
    good_rate for this intent clears the bar (from advise.ranked — $0, no LLM), swaps `model` to it, and PINS it so
    the bandit cannot re-swap. With no measured evidence for the intent it keeps the named model (honest); with
    no_substitution it titrates EFFORT for the named model only. The chosen arm is stamped substituted_from/best_value
    so the saving+decision are booked against the counterfactual (what the named model would have cost).

    PARAMS a consumer commonly passes (all optional): `timeout_s` bounds the request — a client-side cancel that
    actually STOPS the call (and its billing), not merely the wait; it applies to BOTH the lane and the API path.
    `schema` (a JSON Schema) forces STRUCTURED output — a forced tool-call on anthropic, response_format
    json_schema/json_object elsewhere. `no_metered_fallback=True` makes a lane MISS return an error instead of
    paying the API ($0 by construction). `no_substitution=True` PINS THE VENDOR: it suppresses BOTH lane-bandit
    load-balancing (proactive) and lane-failover substitution (reactive), so the requested model answers or errors —
    never a silent swap. Use it whenever the VENDOR IS THE MEASUREMENT (a cross-vendor consensus panel, a 2-judge
    adjudication): the bandit in optout mode can otherwise run one model in place of four while every result keeps
    its requested label. `images=[path | data: URL, …]` sends a VISION request — each image is
    loaded once, priced by the PIXEL rule (content_tokens, not the text tokenizer), and the call rides the
    metered API (the subscription lanes are text-only CLIs, so a vision call skips them: executor='api').

    RETURNS a dict — NEVER raises — with the SAME keys on success and failure, so a caller never has to guess:
      text          the answer (str), or None on any failure / a reply truncated past its retry
      cost          $ for THIS call: 0.0 = served by a $0 subscription LANE, a positive number = metered API,
                    None = refused/errored (nothing spent)
      executor      WHICH path served or attempted it — a lane name ('claude-code' | 'codex' | 'gemini' |
                    'zai-coding') or 'api' (the metered provider). This is how a caller tells LANE-vs-API from
                    the result alone, on success AND on error.
      in_tok/out_tok/latency/finish_reason  usage + why the reply ended (finish_reason 'length' = truncated)
      substituted_from  present when a lane miss was served by a DIFFERENT lane/model (provenance)
      on FAILURE (error is non-None): error (one-line message), error_type (the exception CLASS — e.g.
                    APITimeoutError [deadline] vs APIConnectionError [transport] vs NotFoundError [bad model]),
                    status_code (HTTP status if any), provider_error (the real response BODY, not the one-line
                    str), cause (the underlying error behind a generic wrapper — 'Connection error.' ←
                    'ConnectTimeout'), retry_after (seconds, if the provider sent one)."""
    # metered_only FORCES the exact model on the metered API, so it must ALSO pin the model: a substitution (best-value,
    # or a lane-bandit swap) would silently route to a DIFFERENT vendor's $0 lane, defeating BOTH the model choice and
    # the metered intent. That is the measured warden:tag_provenance_xcheck bypass — metered_only=True still yielded
    # gpt-5-nano → claude-opus-4-8 via the claude-code lane. metered_only ⟹ no_substitution closes it; there is no
    # legitimate metered_only-WITH-substitution case (every metered_only caller wants that exact model, metered).
    if metered_only:
        no_substitution = True
    # ENSURE-SUCCESS INPUT NORMALISATION — accept the kwargs a caller NATURALLY reaches for and translate them to the
    # canonical parameter, instead of a cryptic TypeError (the "users try to use it and fail" class, caught by a live
    # run: a caller wrote reasoning="best-value", intent=… and got `unexpected keyword argument 'intent'`). Aliases are
    # unambiguous RENAMES; the canonical param wins if BOTH are given; a genuinely unknown kwarg fails LOUDLY with
    # guidance — never silently swallowed, because a dropped generation param is a silent wrong-result.
    if aliases:
        _ALIASES = {"effort": "reasoning", "reasoning_effort": "reasoning",
                    "max_output_tokens": "max_tokens", "max_completion_tokens": "max_tokens"}
        # `governed` is a REAL feature kwarg carried via **aliases (the docstring documents it; it is popped at the
        # dispatch-governor step below), NOT a rename and NOT an unknown. It must be skipped here or the documented
        # governed=True concurrent-fan path — and every governed=False single call — dies with a spurious
        # 'unexpected keyword' TypeError before the pop ever runs (the reject loop preceded the pop).
        _FEATURE_KW = {"governed"}
        _canon = {}
        for _k in list(aliases):
            if _k in _FEATURE_KW:
                continue
            _c = _ALIASES.get(_k)
            if not _c:
                raise TypeError(
                    "call() got an unexpected keyword %r. Pass: model, prompt, max_tokens (alias "
                    "max_output_tokens/max_completion_tokens), reasoning=minimal|low|medium|high|best-value (alias "
                    "effort/reasoning_effort), intent (alias sig), system, schema, timeout_s, files, images, "
                    "no_metered_fallback, no_substitution, retries. Generation knobs like temperature/top_p/stop are "
                    "not surfaced by the governance layer." % _k)
            _canon[_c] = aliases[_k]
        if "reasoning" in _canon and reasoning is None:
            reasoning = _canon["reasoning"]
        if "max_tokens" in _canon and max_tokens is None:
            max_tokens = _canon["max_tokens"]

    # `intent` is the caller-facing ALIAS for `sig` (the job-type label advise/best-value/attribution key on). A
    # caller who reaches for intent= — the natural thing to pass for reasoning="best-value" — gets it honoured, not a
    # TypeError, and the same tag fixes the "PAID call with NO intent → (none)" attribution gap. When both are given,
    # sig names the finer call-class (its measured p99 sizes the output budget) and intent the job-type (best-value
    # below prefers it explicitly); when only intent is given it becomes the sig too, so one tag drives everything.
    if intent is not None and not sig:
        sig = intent

    # METERED_ONLY ⟹ NO_SUBSTITUTION. A metered_only caller is a JUDGE whose verdict must be REPRODUCIBLE on the model
    # it named — a cross-vendor panel where the MODEL is the measurement. Skipping the $0 lane (metered_only's own job,
    # applied at dispatch) is NOT enough on its own: the best-value / utilisation bandit picks the model EARLIER than
    # lane routing, so an un-pinned metered_only call gets its model swapped anyway (measured: a warden cross-check named
    # gpt-5-nano and was served claude-opus-4-8 via the claude-code lane), collapsing the cross-vendor panel to one
    # vendor — and the swapped model's reply then failed to scatter, so every verdict came back empty. Pinning the vendor
    # for a reproducibility-critical caller cannot harm it and closes the 2026-08-29 "cross-vendor panel was one model"
    # gap at its structural root, not per-intent in a denylist.
    if metered_only:
        no_substitution = True

    # BASE-FALLBACK DEFAULT (tier-3 reliability). Auto-ON so a call yields SOME answer when a model's lane AND its
    # metered API both fail — EXCEPT where the model IS the measurement (no_substitution / metered_only pins it) or a
    # tiny probe, and except when the operator turns it off (advisor.base_fallback_default=false). An explicit
    # base_fallback= from the caller always wins (None = "decide here"). Same-provider by construction
    # (provider_base_model), so a pinned panel/judge is never silently moved to a different vendor. Read the caller's
    # ORIGINAL pinning here (before a lane-shed may set metered_only), so a shed call still gets the safety net.
    if base_fallback is None:
        try:
            _bf_default = str(config._cfg_get("advisor", "base_fallback_default", True)).strip().lower() \
                not in ("0", "false", "no", "off")
        except Exception:
            _bf_default = True
        base_fallback = _bf_default and not (no_substitution or metered_only or _probe)

    # INPUT-COMPLETENESS: fold whole, stamped, self-verified files into the prompt BEFORE the guards, so the
    # full payload is what _input_fits measures and a size overflow is refused here rather than clipped by the
    # vendor. Consumed here (not forwarded), so the _call_guarded → call(_no_guard=True) recursion below never
    # re-assembles. attach_many fails closed (PartialFileError) — a partial file never reaches a provider.
    if files:
        from . import llm_files
        _block, _ = llm_files.attach_many(files)
        prompt = _block + "\n" + prompt
    if images:
        # load each image ONCE (path/data-URL → {data_uri, media_type, b64, w, h}); the loaded dicts thread through
        # so the input estimate and the request build never re-read the file. A vision call rides the metered API,
        # not a subscription lane (the lane CLIs are text-only) — _call_once forces that.
        images = [_load_image(i) for i in images]
    # AGENTIC MODEL RESOLUTION (transparent, cached) — do it HERE so the budget, guards, and dispatch all use the
    # SERVED id. If the vendor does not serve the requested id, an LLM picks the served model that best delivers what
    # was asked (a phantom or renamed id: gpt-5.6 → the served gpt-5.6-sol; gemini-3-flash → gemini-3-flash-preview).
    # It only ever CHANGES an id the vendor would otherwise reject, so a served id is untouched ($0 cache read); the
    # swap is RECORDED on the result (resolved_from/resolution) — never silent, never cross-VENDOR. Skipped inside the
    # resolver's own advisor call (_resolve_guard) so it cannot recurse. A DELIBERATE stop from the tiny resolver call
    # propagates (never a silent guess); any other resolver failure leaves the id as-is (the honest unserved path).
    _resolved_from = _resolution = None
    if not getattr(_resolve_guard, "on", False):
        try:
            from . import vendor_call as _vcres
            _rprov = provider_for(model)               # raises for a bare id with no registered provider
            _rraw = model.split(":", 1)[1] if ":" in model else model
            _rid, _rreason = _vcres.served_substitute(_rprov, _rraw)
            if _rid and _rid != _rraw:                 # a served substitute was chosen → use it, record the swap
                _resolved_from, _resolution, model = f"{_rprov}:{_rraw}", _rreason, f"{_rprov}:{_rid}"
        except Exception as _rex:
            from . import gate as _rgate
            if isinstance(_rex, _rgate.deliberate_stop_types()):
                raise                                  # a spend/budget refusal or deadline HALTS — never a silent guess
            # unknown provider / resolver hiccup → leave the id UNCHANGED and proceed (the honest unresolved path)
    # BEST-VALUE RESOLUTION — reasoning="best-value" delegates the (model, effort) choice to the MEASURED frontier:
    # the cheapest (model, effort) whose recorded good_rate for this intent clears the bar (best_value.resolve). It is
    # $0 and makes NO LLM call — the quality was already judged+recorded, so picking the cheapest arm that holds is
    # arithmetic on that evidence, not a fresh meaning call. Skipped for a probe or inside the resolver's own call.
    # Degrades HONESTLY: no measured evidence → keep the caller's model. The sentinel is CONSUMED here either way — it
    # must never reach the wire as a literal reasoning_effort (that would 400). A resolved choice is PINNED
    # (no_substitution) so the utilisation bandit cannot re-swap the model best-value deliberately chose.
    # CONFIG-SCOPED BEST-VALUE DEFAULT (opt-in; OFF unless advisor.default_reasoning='best-value'). A labelled,
    # unpinned, non-probe call that set no reasoning is defaulted to best-value here — so DELEGATED work auto-routes
    # to the measured cost×quality frontier without every caller opting in. No-op in every other case; the sentinel
    # it may set is consumed just below exactly like an explicit reasoning="best-value".
    # Capture whether best-value was EXPLICIT (the caller passed reasoning="best-value", delegating the model choice)
    # vs about to be INHERITED from advisor.default_reasoning — read BEFORE the default is applied, while `reasoning`
    # still says which. Drives the floor-preserving pin below.
    _bv_explicit = isinstance(reasoning, str) and reasoning.strip().lower() == "best-value"
    reasoning = _apply_best_value_default(reasoning, intent, sig, no_substitution, _probe)
    _bv_from = _bv_why = _bv_effort = None
    if isinstance(reasoning, str) and reasoning.strip().lower() == "best-value":
        reasoning = None                               # consume the sentinel regardless of the outcome below
        if not _probe and not getattr(_resolve_guard, "on", False):
            from . import gate as _bvg                 # imported OUTSIDE the try so the deliberate-stop except below
            #                                            can ALWAYS be evaluated (never a NameError on an import hiccup)
            try:
                from . import best_value as _bv, calls as _bvc
                _bv_intent = intent or (_bvc.current() or {}).get("intent") or sig
                # FLOOR-PRESERVING PIN — best-value must be floor-IMPROVING, never floor-LOWERING. The global DEFAULT
                # (advisor.default_reasoning=best-value, INHERITED by a call that pinned model= and never asked for a
                # swap) titrates EFFORT for the pinned model ONLY (pin_model=True) — it NEVER re-picks the model, and
                # never spends an extra recommend_models call to return a cold-intent "no pick" for that caller. Only
                # an EXPLICIT reasoning="best-value" delegates the model choice (pin_model=no_substitution). Either way
                # "no data yet" degrades to the caller's OWN model at a sane effort (a normal result), never to
                # "no pick -> no answer". (An explicit, UNPINNED best-value still infers an intent from the prompt.)
                _pick = _bv.select_model_effort(_bv_intent, model, pin_model=(no_substitution or not _bv_explicit),
                                                prompt=prompt)
            except _bvg.deliberate_stop_types():
                raise                                  # a spend refusal / deadline from the advisor PROPAGATES as a
                #                                        TYPED stop (a consumer can distinguish it) — NEVER swallowed to
                #                                        _pick=None and an empty result the gate intended to stop
            except Exception:
                _pick = None                           # any OTHER advisor hiccup -> degrade to the named model (floor-preserving)
            import sys as _sbv
            if _pick and _pick.get("model"):
                _bv_from, _bv_why, _bv_effort = model, _pick.get("why"), _pick.get("effort")
                model, reasoning, no_substitution = _pick["model"], _pick.get("effort"), True
                print(f"[spendguard] {_bv_why} (was {_bv_from})", file=_sbv.stderr)
            elif _pick is not None and _pick.get("why"):
                print(f"[spendguard] {_pick['why']}", file=_sbv.stderr)   # honest no-pick: keep the named model
    # ATTRIBUTION: a caller that passed a sig/intent but is NOT inside a `with calls.context(...)` must still have
    # THIS call's ledger row tagged. gate._record_rt reads the THREAD-LOCAL intent, and sig= alone never set it — so a
    # metered call passing only sig= landed in '(none)' despite the tag, the exact gap the sig/intent alias claims to
    # close (a PAID call with no intent is invisible to advise/denylists/rollups). Set it for the dispatch and restore
    # the prior context EXACTLY afterwards, so it never leaks past this call. An ambient context intent WINS — the
    # caller's explicit `with calls.context(...)` is never overridden — and a $0/lane call is tagged the same way.
    from . import calls as _sig_ctx
    _ctx_before = dict(_sig_ctx.current() or {})
    if sig and not _ctx_before.get("intent"):
        _sig_ctx.set_context(intent=sig)
    # GOVERNED (opt-in `governed=True`, for a CONCURRENT fan) — enter the dispatch governor with the SAME
    # shed-to-metered + deadline-split policy vendor_call uses, via the shared dispatch.admit brain, so the two
    # entries can never drift. A saturated $0 lane SHEDS to its metered twin (lane-first, not a per-call metered
    # spill); a saturated METERED vendor (or no_metered_fallback) returns a TYPED deadline dict. Single calls omit
    # governed (no governor overhead). Never governs the internal _no_guard recursion — one slot per logical call.
    # ADMISSION — enter the dispatch governor (concurrency + RPM + TPM pacing) so this call cannot 429, via the ONE
    # shed-to-metered brain (dispatch.admit) that vendor_call and the governed fan share, so the entrances never drift.
    # TWO ways in:
    #   · governed=True — the explicit opt-in for a CONCURRENT fan: shed=True (a saturated $0 lane sheds to its paid
    #     twin for throughput).
    #   · MANAGED SERIAL (dispatch.manage_all, default ON) — a plain LABELLED call that is NOT already inside an outer
    #     dispatch slot (a bulk_delegate runner already acquired → dispatch.holding() → one admission per logical call,
    #     no double-take) and is not the _no_guard recursion / a probe. This is what closes the gap that let a serial
    #     caller — or a consumer's own thread pool — blow a provider's tokens/minute ceiling and 429. shed=False here:
    #     a lone call must NEVER be silently moved to the paid API to escape a busy lane (surprise spend) — it queues
    #     within its deadline, else returns a typed deadline dict. (_manage_all_enabled() is checked LAST — a config
    #     read — so unlabelled / held / probe / _no_guard calls short-circuit before paying for it.)
    from . import dispatch
    _governed = bool(aliases.pop("governed", False))
    _adm = None
    _managed = (not _governed and not _no_guard and not _probe and bool(intent or sig)
                and not dispatch.holding() and _manage_all_enabled())
    if (_governed or _managed) and not _no_guard:
        _gprov = provider_for(model)
        _gov_dl = float(timeout_s) if timeout_s else (deadline_for(
            model, intent=intent or sig, in_chars=len(prompt or ""), default_s=LANE_MIN_TIMEOUT_S)[0] or LANE_MIN_TIMEOUT_S)
        _adm = dispatch.admit(_gprov, model, _gov_dl, no_metered_fallback=no_metered_fallback,
                              est_tokens=_est_call_tokens(prompt, system, max_tokens), shed=_governed,
                              skip_lane=bool(metered_only))   # metered_only hits the paid vendor → pace on its RPM/TPM
        if not _adm.ok:
            _sig_ctx._local.ctx = _ctx_before
            return {"provider": _gprov, "model": model, "text": None, "parsed": None, "in_tok": 0, "out_tok": 0,
                    "cost": None, "latency": 0.0, "finish_reason": None, "truncated": None, "executor": None,
                    "max_tokens_used": None, "substituted_from": None,
                    "error": f"dispatch deadline_exceeded ({'governed' if _governed else 'managed'}): {_adm.error}"}
        if _adm.shed:                                    # saturated $0 lane shed to its metered twin → run metered_only
            metered_only = True
    # ROUTE-THROUGH-QUEUE (advisor.route_through_queue, default OFF, DORMANT): record every LABELLED synchronous call
    # durably (a leased row = observability + crash-recovery + priority/SLA metadata) and run it via its NORMAL path —
    # model + lane preference UNCHANGED (this RECORDS + manages the call, it does not re-execute it). Not for the
    # internal _no_guard recursion, a probe, an UNLABELLED call, or a call already inside a routed record (_route_guard
    # → one row per logical call, no double-record and no loop). A deliberate stop from the durable write (ledger lock /
    # refusal) PROPAGATES; a non-deliberate open failure → run UNRECORDED (the queue never gates a caller's result).
    # `_route=False` (an EXPLICIT opt-out) also suppresses the durable record — the caller is ALREADY a governed
    # queue row (lane_queue.drain/submit run the leased rows through bulk_delegate, whose per-task adapters.call
    # would otherwise open a SECOND row for work that is already queued). Same one-row-per-logical-call guarantee as
    # the _route_guard re-entry check, but explicit + thread-safe across bulk_delegate's nested hedge threads (a
    # thread-local set in the worker never reaches the hedge's sub-threads; an explicit flag does).
    _routed = (_route and (intent or sig) and not _no_guard and not _probe
               and not getattr(_route_guard, "on", False) and _route_through_queue_enabled())
    _qrid = None
    r = None
    if _routed:
        _route_guard.on = True
    try:
        if _routed:
            from . import lane_queue as _route_lq
            _qrid = _route_lq.record_open(intent or sig, prompt)
        try:
            if not _no_guard:
                r = _call_guarded(model, prompt, max_tokens=max_tokens, system=system, reasoning=reasoning,
                                  schema=schema, timeout_s=timeout_s, sig=sig, retries=retries,
                                  no_metered_fallback=no_metered_fallback, images=images, _no_sub=no_substitution,
                                  metered_only=metered_only, _probe=_probe)
            else:
                r = _call_once(model, prompt, max_tokens=max_tokens, system=system, reasoning=reasoning,
                               schema=schema, timeout_s=timeout_s, no_metered_fallback=no_metered_fallback, images=images,
                               _no_sub=no_substitution, _skip_lane=metered_only)   # metered_only=True → skip the lane
        finally:
            _sig_ctx._local.ctx = _ctx_before   # restore the caller's context exactly (nested calls keep their own tag)
            if _adm is not None:
                _adm.release()                  # free the governor slot (idempotent; no-op when ungoverned)
    finally:
        if _routed:
            _route_guard.on = False             # always clear the re-entry guard (even if record_open raised a stop)
            if _qrid is not None:
                from . import lane_queue as _route_lq2
                _route_lq2.record_close(_qrid, r if isinstance(r, dict) else {"error": "call produced no result"})
    # TIER-3 — PROVIDER BASE-MODEL FALLBACK (the last hop of lane->metered->base). When the chosen model's whole
    # lane->metered attempt STILL errored on an AVAILABILITY failure, retry the SAME provider's RELIABLE BASE model so
    # the call yields SOME answer instead of an error — clearly LABELLED. base_fallback is default-ON (resolved above),
    # OFF for a pinned/measurement call (no_substitution / metered_only) and a probe; an explicit caller value wins.
    # It fires ONLY on an availability error, NOT on a TRUNCATION: a truncated reply means the model WAS reached and
    # its answer was too long — a base model cannot fix output size, and swapping would corrupt the "surfaced as
    # text=None, in ONE attempt" contract (a jumbo reply must stay text=None, not silently become a base answer).
    # Config/catalog-gated (no provider_base -> no-op), billing-gated (no_metered_fallback -> skip), recursion-guarded
    # (the base retry passes base_fallback=False), and only on the NORMAL path (never the internal _no_guard recursion).
    if base_fallback and not _no_guard and isinstance(r, dict) and r.get("error") and not r.get("truncated") \
            and not no_metered_fallback:
        _bprov = provider_for(model)
        _base = provider_base_model(_bprov)
        if _base and _base != model and f"{_bprov}:{_base}" != model:
            _rb = call(f"{_bprov}:{_base}", prompt, max_tokens=max_tokens, system=system, reasoning=reasoning,
                       schema=schema, timeout_s=timeout_s, sig=sig, no_substitution=True, base_fallback=False,
                       no_metered_fallback=no_metered_fallback)
            if isinstance(_rb, dict) and _rb.get("text") and not _rb.get("error"):
                r = {**_rb, "substituted_from": model, "substitution": f"tier-3 base fallback: {model} unavailable",
                     "base_fallback": True}
    # BEST-VALUE PROVENANCE — record what the caller WOULD have run (the baseline) vs what best-value chose, so the
    # saving/decision booking downstream can price the counterfactual. Stamped only when best-value actually changed
    # the target; never overwrites a lane/bandit substituted_from already present. requested_effort is None (the
    # sentinel baseline is the named model at its DEFAULT effort); chosen_effort is the titrated pick.
    if _bv_from and isinstance(r, dict):
        r = {**r, "substituted_from": r.get("substituted_from") or _bv_from,
             "substitution": r.get("substitution") or _bv_why, "best_value": True,
             "requested_effort": None, "chosen_effort": _bv_effort}
    _book_substitution(r)   # book the DECISION (value proof + learner evidence) + any metered→cheaper saving
    # `billed` (cost>0) is NOT "served by the metered API": a per-token key LANE (e.g. zai-coding) costs while still
    # being lane-served, so a caller proving refuse_billed/$0 via `billed` gets false positives. This is the field to
    # reach for instead — the metered provider API served it (a lane miss fell through, or there was no lane): executor
    # 'api'/'api-fallback', or (defensively) a cost with no lane executor recorded.
    if isinstance(r, dict):
        if _resolved_from:                            # TRANSPARENCY: the caller asked for one id and a served one
            r["resolved_from"] = _resolved_from       # answered — surface BOTH, so the swap is auditable, never silent
            r["resolution"] = _resolution
        _ex = r.get("executor")
        r["served_by_metered_api"] = _ex in ("api", "api-fallback") or (not _ex and bool(r.get("cost")))
        _warn_once_if_substituted(r)                   # a NAMED model silently swapped → loud once (measurement footgun)
        # STRUCTURED OUTPUT: when a schema was requested, surface the DECODED object alongside `text` (provenance
        # kept — purely additive), so N consumers don't each re-json.loads + reinvent a salvage; the shape gate
        # already parsed this body. `parsed` is the object, or None if it did not decode (undecodable → the caller
        # sees None, not the silent-nothing that reads as "the model had little to say").
        if schema is not None and r.get("text") and not r.get("error"):
            try:
                from . import output_contract as _oc
                _obj, _ = _oc._as_obj(r["text"])
                r["parsed"] = _obj if isinstance(_obj, (dict, list)) else None
            except Exception:
                r["parsed"] = None
        # CORPUS CAPTURE (opt-in, callio.capture_live): record this WORKLOAD call's prompt+output into the call_io
        # replay corpus, so bakeoff / effort-titrate / advise good% have real tasks to sample WITHOUT a batch to
        # fetch — the path that unblocks measuring realtime/lane-only intents (an estate whose work never became a
        # provider batch had no bodies for fetch-io to recover). OUTER call only (not the _no_guard recursion, so one
        # capture per call), never a probe, only a SERVED (no-error) call. capture_live is opt-in, workload-only,
        # bounded, LOUD where it stops, and NEVER raises into the call.
        if not _no_guard and not _probe and not r.get("error"):
            from . import callio, calls as _cap_ctx
            callio.capture_live(sig or (_cap_ctx.current() or {}).get("intent"), r.get("provider") or "",
                                r.get("model") or model, prompt, r.get("text"),
                                in_tok=r.get("in_tok") or 0, out_tok=r.get("out_tok") or 0,
                                system=system, req_schema=schema, req_max_tokens=max_tokens)
    return r


def structured_reply(r):
    """The decoded structured object from a call result `r`, as a dict, or None — the ONE way a consumer reads a
    schema/structured reply. `call()` already decodes the reply FENCE-TOLERANTLY (via output_contract._as_obj) and
    surfaces it as r['parsed']; a consumer must read THAT, not re-json.loads the raw text. Nine judge/parse sites
    used to read a NONEXISTENT r['json'] (the adapter never sets that key → always None) and fall back to a
    fence-BLIND json.loads(text), so a $0 lane whose reply is wrapped in a ```json … ``` fence never parsed and the
    judge SILENTLY LABELED NOTHING (the absence-as-success failure this repo exists to prevent). Reading the
    adapter's own decode fixes every one. The _as_obj branch is a belt-and-suspenders fallback for a result that
    reached a non-schema path (no 'parsed'); it is still fence-tolerant, never a bare json.loads."""
    obj = r.get("parsed")
    if isinstance(obj, dict):
        return obj
    if obj is None and r.get("text"):
        try:
            from . import output_contract as _oc
            obj, _ = _oc._as_obj(r["text"])
        except Exception:
            obj = None
    return obj if isinstance(obj, dict) else None


def vision(model, prompt, images, *, schema=None, system=None, sig=None, reasoning=None,
           max_tokens=None, timeout_s=None, retries=2, no_metered_fallback=False, no_substitution=False):
    """Run one VISION prompt (image(s) + text) against a vision-capable model — the explicit, discoverable entry
    for what `call(images=…)` does, so a vision request is never accidentally sent as a text call.

    WHY A NAMED ENTRY. Vision has one non-obvious rule: the subscription LANES are text-only print-mode CLIs
    (agy/codex/claude-code) with NO image channel, so a vision call must ride the metered API. `call` already does
    this — an `images=` argument skips the lane by construction (executor='api') and prices each image by PIXELS
    (content_tokens), not the text tokenizer. But that lived as one kwarg on a long signature, easy to miss: a
    caller who did not find it embedded the image elsewhere, the request rode a text lane, and the CLI cold-400'd
    or returned an empty/garbled reply. This entry makes the capability impossible to miss and REFUSES to silently
    degrade — `images` is required and must be non-empty (a vision call with no image is a bug, not a text call).
    Each image is a file PATH or a data: URL. NB: `no_substitution` does NOT route a call to the API — only
    `images` skips the lane; that mix-up (no_substitution as a vision fix) is why this entry exists.

    SCHEMA. Pass `schema` for structured output, but on OpenAI models keep it strict-expressible: a DYNAMIC-KEY MAP
    (labels:{<id>:<label>}) is collapsed to {} by strict mode and comes back empty — use an ARRAY of {key,value}
    objects (results:[{id,label}]). `call` refuses such a map with a typed SchemaNotStrictExpressible error rather
    than returning the empty object, so the failure is loud, not a plausible blank.

    Returns the same result dict as `call` (executor='api'); raises ONLY the empty-images guard."""
    imgs = list(images or [])
    if not imgs:
        raise ValueError(
            "vision(images=…) requires at least one image (a file path or data: URL). A vision call with no image "
            "would silently become a TEXT call routed to a text-only lane — the exact failure this entry prevents. "
            "Use adapters.call(...) for a text-only prompt.")
    return call(model, prompt, images=imgs, schema=schema, system=system, sig=sig, reasoning=reasoning,
                max_tokens=max_tokens, timeout_s=timeout_s, retries=retries,
                no_metered_fallback=no_metered_fallback, no_substitution=no_substitution)


_EMBED_MAX_BATCH = 128            # inputs per /embeddings request — conservative, well under provider caps (OpenAI
#                                   2048, Gemini smaller); overridable via max_batch=. A NAMED default, never magic.
_EMBED_TIMEOUT_S = 60             # per-request wall bound so a hung embeddings call can't wedge a whole corpus run.
_EMBED_MAX_INPUT_CHARS = 32000    # ~8k-token per-input ceiling (OpenAI) as a conservative CHAR guard — an oversized
#                                   text is marked failed for THAT item, never sent to 400 the whole chunk it rides in.
_DEFAULT_EMBED_MODEL = "text-embedding-3-small"   # the fallback embed model (cheap OpenAI) when none is given and
#                                                   config advisor.embed_model is unset — OpenAI is the default we use.


def _embed_default_model():
    """The embed model to use when a caller passes none: config `advisor.embed_model` if set, else the named OpenAI
    default — resolved, never a bare literal at the call site (change the default in ONE place / via config)."""
    try:
        return config._cfg_get("advisor", "embed_model", None) or _DEFAULT_EMBED_MODEL
    except Exception:
        return _DEFAULT_EMBED_MODEL


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
    from openai import OpenAI
    return (OpenAI(api_key=key, base_url=spec["base_url"], timeout=_http_timeout(timeout_s), max_retries=0)
            if timeout_s else OpenAI(api_key=key, base_url=spec["base_url"]))


def embed(texts, model=None, *, dimensions=None, max_batch=None, timeout_s=None, checkpoint=None):
    """The first-class REALTIME embedding surface — embed a list of texts on an OpenAI-COMPATIBLE provider (openai
    + gemini today; both speak /embeddings), gated + priced + metered exactly like adapters.call (the gate already
    intercepts .embeddings.create). `model` defaults to the OpenAI default (config advisor.embed_model, else
    text-embedding-3-small); pass a gemini id (e.g. 'gemini-embedding-001') to embed there, so the SAME corpus can
    be embedded on both for an A/B.

    ROBUST BULK (the CHUNK-never-single-shot rule, all four parts):
      • CHUNKED into requests of at most `max_batch` inputs;
      • DURABLE BY DEFAULT — a MULTI-chunk run auto-checkpoints to a jsonl under HOME (a single request is atomic;
        more than one is crash-losable), each chunk APPENDED (keyed by sha256(text) — CONTENT, not position) BEFORE
        the next runs, so a crash RESUMES and never re-pays, and a changed list re-embeds only new texts;
      • per-request TIMEOUT (default _EMBED_TIMEOUT_S) so a hung call can't wedge the run;
      • per-input SIZE GUARD + per-chunk ISOLATION — an oversized text is marked failed for that item (never sent to
        400 its chunk), and one chunk's failure marks its items and KEEPS GOING (the rest still embed + checkpoint).

    Returns a dict — NEVER raises (except a deliberate spend stop, which PROPAGATES) — {vectors, model, dims, n,
    checkpoint, failed, error}. `vectors` is ALIGNED to inputs (None where an item failed) and `failed` lists
    {i, reason}; it is never a SILENTLY short list — a None + a failed[] entry, or an explicit error. Re-run to
    retry only the failed items (the checkpoint already holds the rest)."""
    import json as _json, hashlib as _hl
    from . import gate as _gp
    model = model or _embed_default_model()
    prov = _gp._provider_of(model)               # provider_for (prefix) → priced catalog; the canonical resolver
    raw = model.split(":", 1)[1] if ":" in model else model
    items = list(texts or [])
    timeout_s = timeout_s or _EMBED_TIMEOUT_S
    base = {"vectors": [], "model": raw, "dims": dimensions, "n": len(items),
            "checkpoint": checkpoint, "failed": [], "error": None}
    if not prov or prov == _gp.UNKNOWN_PROVIDER:
        return {**base, "error": f"cannot resolve a provider for {model!r} — pass 'provider:model'"}
    if not items:
        return base
    keys = [_hl.sha256(t.encode("utf-8", "replace")).hexdigest() for t in items]
    _n = max(1, int(max_batch or _EMBED_MAX_BATCH))
    if checkpoint is None and len(items) > _n:
        _ck = _hl.sha256(f"{raw}|{len(items)}|{keys[0]}|{keys[-1]}".encode()).hexdigest()[:10]
        checkpoint = str(config.HOME / f"embed_{raw}_{_ck}.jsonl")    # durable by default for a multi-request run
    base["checkpoint"] = checkpoint
    done = {}                                                          # sha(text) -> vector, seeded from the checkpoint
    if checkpoint:
        try:
            with open(checkpoint) as fh:
                for line in fh:
                    line = line.strip()
                    if line:
                        row = _json.loads(line)
                        done[row["k"]] = row["v"]
        except FileNotFoundError:
            pass
    _extra = {"dimensions": dimensions} if dimensions else {}
    from . import gate as _g
    _stop = _g.deliberate_stop_types()
    try:
        c = _oai_compat_client(prov, timeout_s)
    except Exception as e:
        return {**base, "error": str(e)[:200]}                        # client build (unknown provider / no key)
    failed = []
    todo = []                                                         # not-yet-embedded AND within the size guard
    for i, k in enumerate(keys):
        if k in done:
            continue
        if len(items[i]) > _EMBED_MAX_INPUT_CHARS:
            failed.append({"i": i, "reason": f"input {len(items[i])} chars > {_EMBED_MAX_INPUT_CHARS} cap — split it"})
        else:
            todo.append(i)
    for c0 in range(0, len(todo), _n):
        grp = todo[c0:c0 + _n]
        try:
            r = c.embeddings.create(model=raw, input=[items[i] for i in grp], **_extra)  # gate meters this
        except Exception as e:
            if isinstance(e, _stop):
                raise                                                 # a spend refusal / cap HALTS — never isolated
            failed.extend({"i": i, "reason": str(e)[:120]} for i in grp)  # ISOLATE: mark the chunk, keep going
            continue
        vecs = {int(d.index): list(d.embedding) for d in r.data}      # index is CHUNK-relative → map to the group
        _fh = open(checkpoint, "a") if checkpoint else None           # append this chunk BEFORE the next → durable
        try:
            for j, i in enumerate(grp):
                v = vecs.get(j)
                if v is None:
                    # the provider accepted the input but returned NO vector for it (a short r.data) — record WHICH
                    # item, so every None in `vectors` has a failed[] entry (the 'never a silently-short list' rule).
                    failed.append({"i": i, "reason": "provider returned no vector for this input (omitted from r.data)"})
                    continue
                done[keys[i]] = v
                if _fh:
                    _fh.write(_json.dumps({"k": keys[i], "v": v}) + "\n")
        finally:
            if _fh:
                _fh.close()
    out = [done.get(k) for k in keys]
    n_missing = sum(1 for v in out if v is None)
    dims = next((len(v) for v in out if v is not None), dimensions)
    return {**base, "vectors": out, "dims": dims, "failed": failed,
            "error": (f"{n_missing}/{len(items)} inputs unembedded (see failed[]); re-run to retry only those"
                      if n_missing else None)}


def embed_batch(texts, model=None, *, dimensions=None, jsonl_path=None, cap_dollars=None, submit=True):
    """The first-class BATCH embedding surface — the ASYNC Batch API (~50% cheaper) for a LARGE corpus, gated
    estimate-first + capped by the SAME chokepoint every batch submission passes (submit.guarded_submit). Builds a
    /v1/embeddings JSONL (one input per line, custom_id 'emb-<i>' = input index) at a durable path, estimates +
    cap-checks it ($0, no paid call), then submits. Returns {batch_id, jsonl, requests, error}; submit=False
    writes + estimates only. Collect results later with callio.guarded_collect(batch_id) — vectors map by custom_id.

    Batch availability is a CONFIG CAPABILITY on the provider spec (`batch`), never a hardcoded id check: OpenAI
    declares it (its async Batch API). A provider that does not (gemini's OpenAI-compat endpoint exposes no
    /batches — its async batch is a separate native API) returns a clear error pointing to embed() (chunked
    realtime) until its native batch path is wired — a LOUD boundary, never a silent metered fallback."""
    import json as _json, hashlib as _hl
    from . import gate as _gp
    model = model or _embed_default_model()
    prov = _gp._provider_of(model)               # provider_for (prefix) → priced catalog; the canonical resolver
    raw = model.split(":", 1)[1] if ":" in model else model
    items = list(texts or [])
    base = {"batch_id": None, "jsonl": None, "requests": len(items), "error": None}
    if not PROVIDERS.get(prov or "", {}).get("batch"):
        return {**base, "error": f"{prov} does not declare the async Batch API (provider spec 'batch') — use embed() "
                                 f"(chunked realtime) for {prov}, or wire + declare its native batch path."}
    if not items:
        return base
    if not jsonl_path:                                               # a stable, content-derived name (so a re-run reuses it)
        _tag = _hl.sha256(f"{raw}|{len(items)}|{items[0][:80]}|{items[-1][:80]}".encode("utf-8", "replace")).hexdigest()[:10]
        jsonl_path = str(config.HOME / f"embed_batch_{raw}_{_tag}.jsonl")
    try:
        with open(jsonl_path, "w") as fh:
            for i, t in enumerate(items):
                body = {"model": raw, "input": t}
                if dimensions:
                    body["dimensions"] = dimensions
                fh.write(_json.dumps({"custom_id": f"emb-{i}", "method": "POST",
                                      "url": "/v1/embeddings", "body": body}) + "\n")
        from . import submit as _submit
        bid = _submit.guarded_submit(jsonl_path, model=raw, cap_dollars=cap_dollars, batch=True,
                                     submit=submit, endpoint="/v1/embeddings")
        return {**base, "batch_id": bid, "jsonl": jsonl_path}
    except Exception as e:
        from . import gate as _g2
        if isinstance(e, _g2.deliberate_stop_types()):
            raise                                                     # a cap refusal HALTS — never a silent partial
        return {**base, "jsonl": jsonl_path, "error": str(e)[:200]}


_EMBED_COMPARE_SAMPLE = 200       # texts embedded per model for an A/B — a sample is enough for a STRUCTURE compare


def embed_compare(texts, models=None, *, sample=None):
    """A/B two or more embedding models on the SAME texts — the cheap way to decide openai vs gemini (vs voyage)
    BEFORE committing a corpus + a Qdrant collection to one. Embeds a SAMPLE of `texts` on each model (default: the
    OpenAI default + gemini-embedding-001), then reports per-model {dims, rate_per_1m, n_failed} and — PAIRWISE — how
    much the models AGREE on neighbourhood STRUCTURE: the Pearson correlation of their off-diagonal cosine-similarity
    matrices (≈1.0 = the models call the same texts near/far; low = they disagree on what is similar). A quality
    signal that needs NO labelled retrieval set. Makes REAL (gated) embedding calls on the sample — small, not free.

    Returns {sample_n, models: [{model, dims, rate_per_1m, n_failed, error}],
             agreement: [{a, b, structure_corr, pairs}]}."""
    from . import pricing as _pr

    def _cosine_sim(a, b):
        dot = sum(x * y for x, y in zip(a, b))
        na = sum(x * x for x in a) ** 0.5
        nb = sum(y * y for y in b) ** 0.5
        return (dot / (na * nb)) if na and nb else 0.0

    def _pearson(xs, ys):
        n = len(xs)
        if n < 2:
            return None
        mx, my = sum(xs) / n, sum(ys) / n
        cov = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
        vx = sum((x - mx) ** 2 for x in xs) ** 0.5
        vy = sum((y - my) ** 2 for y in ys) ** 0.5
        return (cov / (vx * vy)) if vx and vy else None

    models = list(models) if models else [_embed_default_model(), "gemini-embedding-001"]
    items = list(texts or [])
    _s = int(sample or _EMBED_COMPARE_SAMPLE)
    items = items[:_s] if len(items) > _s else items
    per, sims = [], {}
    for m in models:
        r = embed(items, m)                                          # gated + metered; a sample, so bounded spend
        vecs = r.get("vectors") or []
        try:
            rate = _pr.price(m.split(":", 1)[-1]).get("in_")
        except Exception:
            rate = None
        per.append({"model": m, "dims": r.get("dims"), "rate_per_1m": rate,
                    "n_failed": len(r.get("failed") or []), "error": r.get("error")})
        sims[m] = {(i, j): _cosine_sim(vecs[i], vecs[j])            # off-diagonal cosine over pairs both models have
                   for i in range(len(vecs)) for j in range(i + 1, len(vecs))
                   if vecs[i] is not None and vecs[j] is not None}
    agreement = []
    for a in range(len(models)):
        for b in range(a + 1, len(models)):
            ma, mb = models[a], models[b]
            common = [k for k in sims.get(ma, {}) if k in sims.get(mb, {})]
            agreement.append({"a": ma, "b": mb, "pairs": len(common),
                              "structure_corr": _pearson([sims[ma][k] for k in common],
                                                         [sims[mb][k] for k in common])})
    return {"sample_n": len(items), "models": per, "agreement": agreement}


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
LANE_MIN_TIMEOUT_S = 150.0   # a subscription LANE (CLI cold-start + ~14K context injection for codex, or a plan
                             # HTTP round-trip) is far slower than a metered call, and it is $0 — so handing it a
                             # metered call's tight 30s floor just times it out and churns it to the paid API it
                             # exists to avoid. Floor lane deadlines here; a hung lane is still bounded by its own
                             # TIMEOUT_S. MEASURED: codex timed out at exactly 30s on a real panel file and fell back.
_LANE_RETRY_BACKOFF_S = 1.2  # ON-LANE TRANSIENT RETRY wait: a lane that PROVED it can answer a prompt this size but
#                              failed THIS time is likely a momentary blip (plan throttle / at-capacity) — pause this
#                              long and retry the $0 lane ONCE before spilling to PAID metered. Named, not a literal.


def _http_timeout(timeout_s):
    """An httpx.Timeout that fails FAST on a DEAD endpoint but gives a live-but-slow one the full read budget.

    A bare float handed to the SDK sets connect == read == write == budget, so a vendor that BLACKHOLES the
    connection (measured: z.ai, SYN dropped) held a fan_out slot for the entire budget — up to 600s — just
    trying to connect, and one such vendor made every file in the panel wait that long. Splitting the two so
    CONNECT is short and READ is the budget lets a down vendor be declared unavailable in ~10s while a genuinely
    slow-but-alive one still gets its measured time. Falls back to the plain float if httpx is somehow absent
    (it is a hard dependency of both SDKs, so this is belt-and-suspenders, not a real branch)."""
    if not timeout_s:
        return None
    try:
        import httpx
        return httpx.Timeout(float(timeout_s), connect=min(CONNECT_TIMEOUT_S, float(timeout_s)))
    except Exception:
        return float(timeout_s)


class _CallDeadline(TimeoutError):
    """The WALL-CLOCK deadline fired on an OpenAI-compat generation. A distinct type so the retry ladder re-raises
    it (a timeout is not a bad parameter to drop) and the caller reads it as a deadline, not a transport fault.
    Subclasses TimeoutError so vendor_call's deadline classification recognises it."""


def _exc_detail(e):
    """(http_status, provider_error, retry_after) from a provider SDK exception. STRUCTURED signals only — an HTTP
    status code, the response BODY, and the Retry-After header — never message prose. All best-effort → None when
    absent; vendor_call uses http_status to tell a 429/529 OVERLOAD (retry) from a 400/413 PAYLOAD_REJECTED (do
    not), and honestreview logs provider_error (the real reason lives in the body, not the one-line str(e))."""
    resp = getattr(e, "response", None)
    status = getattr(e, "status_code", None)
    if status is None and resp is not None:
        status = getattr(resp, "status_code", None)
    try:
        status = int(status) if status is not None else None
    except (TypeError, ValueError):
        status = None
    body = getattr(e, "body", None)
    if body is not None and not isinstance(body, str):
        try:
            body = json.dumps(body, default=str)
        except Exception:
            body = str(body)
    if body is None and resp is not None:
        try:
            body = resp.text
        except Exception:
            body = None
    retry_after = None
    if resp is not None:
        try:
            retry_after = resp.headers.get("retry-after")
        except Exception:
            retry_after = None
    return status, (body[:500] if isinstance(body, str) else None), retry_after


def _ratelimit_from_resp(resp):
    """(tpm, rpm) — the vendor's REAL per-minute ceilings, parsed from a provider response's rate-limit headers:
    OpenAI `x-ratelimit-limit-tokens` / `x-ratelimit-limit-requests`, Anthropic `anthropic-ratelimit-tokens-limit` /
    `anthropic-ratelimit-requests-limit` (both are per-minute). Present on a 429, so a single rate-limited call TEACHES
    the governor its limit (dispatch.learn_rate_limit) and the first 429 is the last. Parsing a fixed-format header
    into an int — NOT a meaning judgement. None for a field the provider did not send (or no headers at all)."""
    try:
        h = resp.headers
    except Exception:
        return None, None

    def _i(name):
        try:
            v = h.get(name)
            return int(str(v).strip()) if v not in (None, "") else None
        except (TypeError, ValueError):
            return None

    tpm = _i("x-ratelimit-limit-tokens") or _i("anthropic-ratelimit-tokens-limit")
    rpm = _i("x-ratelimit-limit-requests") or _i("anthropic-ratelimit-requests-limit")
    return tpm, rpm


def _learn_success_limits(provider, resp):
    """Teach the governor a vendor's limit from a SUCCESSFUL response's rate-limit headers — the limit rides
    x-ratelimit-limit-* / anthropic-ratelimit-*-limit on EVERY response, not only the 429, so a vendor is paced BEFORE
    it ever 429s (dispatch.learn persists once per (vendor, limit) — see its change-guard — so this is memory-cheap on
    repeat calls). NEVER loses a paid response to a learning hiccup: a deliberate stop still propagates (it cannot
    arise from the learned-limits JSON writer), any other error is swallowed."""
    try:
        _tpm, _rpm = _ratelimit_from_resp(resp)
        if _tpm or _rpm:
            from . import dispatch as _d
            _d.learn_rate_limit(provider, tpm=_tpm, rpm=_rpm, source="success-header")
    except Exception as _e:
        from . import gate as _g
        if _g.is_deliberate_stop(_e):
            raise
        # else: a successful, PAID response is never discarded for a best-effort side-effect


def _exc_cause(e):
    """The underlying CAUSE chain behind a wrapped SDK exception (`__cause__` / `__context__`), as
    'Type: msg ← Type: msg'. The SDKs wrap a transport failure in a generic APIConnectionError whose str() is
    just 'Connection error.' — the REAL reason (an httpx ConnectTimeout, a DNS failure, a read timeout, a proxy
    refusal) lives in `__cause__`, which _exc_detail cannot see because a transport error carries no `response`
    to read a status/body from. Surfacing it is the difference between a caller told 'Connection error.' and one
    told 'ConnectTimeout: timed out' — the whole point of being transparent to spendguard's consumers. Returns
    None when there is no distinct cause."""
    parts, seen, cur = [], set(), (getattr(e, "__cause__", None) or getattr(e, "__context__", None))
    while cur is not None and id(cur) not in seen and len(parts) < 4:
        seen.add(id(cur))
        msg = str(cur).strip()
        parts.append(type(cur).__name__ + (f": {msg[:120]}" if msg else ""))
        cur = getattr(cur, "__cause__", None) or getattr(cur, "__context__", None)
    return " ← ".join(parts) or None


def _media_type_of(path, raw):
    """image/<fmt> from the file's MAGIC BYTES (fixed signatures), falling back to the extension — a known-format
    parse, not a judgement about content."""
    if raw[:8].startswith(b"\x89PNG"):
        return "image/png"
    if raw[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if raw[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        return "image/webp"
    ext = str(path).rsplit(".", 1)[-1].lower()
    return {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png",
            "gif": "image/gif", "webp": "image/webp"}.get(ext, "image/png")


def _load_image(img):
    """One image → {data_uri, media_type, b64, w, h} from a file PATH or a data: URL (or pass a dict straight
    through, so an image is loaded ONCE and reused by both the input estimate and the request build). Dimensions
    come from the header (content_tokens) — None when unreadable, which the token estimate handles with a flat
    fallback rather than mis-counting the base64 as text (the base64-as-tokens error)."""
    from . import content_tokens as _ct
    if isinstance(img, dict) and img.get("data_uri"):
        return img
    if isinstance(img, str) and img.startswith("data:"):
        split = _ct._split_data_url(img)
        if not split:
            raise ValueError("images=: unparseable data: URL (need data:<media>;base64,<payload>)")
        media, b64 = split
        dims = _ct.dims_from_b64(b64)
        return {"data_uri": img, "media_type": media or "image/png", "b64": b64,
                "w": dims[0] if dims else None, "h": dims[1] if dims else None}
    import base64 as _b64
    with open(img, "rb") as fh:                          # a missing file raises FileNotFoundError — never a silent skip
        raw = fh.read()
    b64 = _b64.b64encode(raw).decode()
    dims = _ct.dims_from_bytes(raw)
    media = _media_type_of(img, raw)
    return {"data_uri": f"data:{media};base64,{b64}", "media_type": media, "b64": b64,
            "w": dims[0] if dims else None, "h": dims[1] if dims else None}


def _image_input_tokens(images, provider, model):
    """INPUT tokens the images add, via content_tokens' provider-aware per-image rule (Anthropic (w×h)/750, OpenAI
    tiles) — NOT the text tokenizer. A flat fallback when dimensions are unreadable."""
    from . import content_tokens as _ct
    total = 0
    for info in images:
        if info.get("w") and info.get("h"):
            total += _ct.image_tokens(info["w"], info["h"], provider=provider, model=model)
        else:
            total += _ct.fallback_image_tokens()
    return total


def _image_parts(images, kind):
    """Provider content parts for the loaded images: OpenAI `image_url` parts, Anthropic `image` base64 blocks —
    the two wire formats content_tokens._media_of already reads back, kept in one place so build and count agree."""
    if kind == "anthropic":
        return [{"type": "image", "source": {"type": "base64", "media_type": i["media_type"], "data": i["b64"]}}
                for i in images]
    return [{"type": "image_url", "image_url": {"url": i["data_uri"]}} for i in images]   # openai / compatible


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
    from . import lane_catalog                           # a BARE base id → the lane's default served tier, never
    return base + "-" + (lane_catalog.quirk("gemini").get("default") or "medium")   # the bare id agy rejects


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
        from . import catalog
        ids = catalog.live_model_ids(provider)             # cached metered served ids (dispatch form), or None
        if not ids or model_id in ids:
            return model_id                                # no list, or already served → nothing to resolve
        variants = sorted(m for m in ids if _is_dated_variant(model_id, m))
        return variants[-1] if variants else model_id      # newest dated variant, else unchanged (stays flagged stale)
    except Exception:
        return model_id                                    # best-effort — a catalog hiccup never breaks the mapping


def metered_fallback_id(provider, use_name):
    """The metered-API model id equivalent to a lane's USE-NAME — the mapping that lets a lane call fall BACK to the
    provider's paid API when the lane is down/exhausted, WITHOUT the lane's own naming breaking the call. This is the
    ONE place that equivalence lives, so the live fallback (_call_once) and the lane-fallback audit can never drift.
    For gemini/agy the reasoning level rides the id as a SUFFIX (gemini-3.7-flash-medium) while the metered API wants
    the BARE id + a reasoning PARAMETER, so the suffix is split off. Every other provider's lane id IS its metered id,
    EXCEPT a bare alias the metered API dates: claude-haiku-4-5 (the CLI's alias) → claude-haiku-4-5-20251001 (the
    served id), resolved $0 from the served-list cache so a pinned claude-haiku call never strands its own fallback.
    Returns (metered_id, reasoning_level_or_None)."""
    if provider == "gemini":
        return _split_gemini_reasoning(use_name)
    return _served_dated_id(provider, use_name), None


def _cacheable_system(system):
    """True when a system prompt is long enough to be worth prompt-caching. A bound on a REAL quantity (its token
    count vs the provider's cache minimum) — a deterministic threshold, not a meaning judgement. Anthropic and OpenAI
    both need ~1024 tokens for a cache block to form; the shared minimum lives in cacheaudit._MIN_CACHE_TOKENS."""
    if not system:
        return False
    from . import cacheaudit
    try:
        import tiktoken
        n = len(tiktoken.get_encoding("o200k_base").encode(system))
    except Exception:
        n = len(system) // 4            # a chars→tokens heuristic is adequate for a threshold when tiktoken is absent
    return n >= cacheaudit._MIN_CACHE_TOKENS


_NOMINAL_OUT_TOKENS = 2000   # output allowance for a call that did not pin max_tokens, for TPM pacing ONLY (input
# dominates TPM for the classification/extraction fans that actually 429; a soft rate bucket needs a ballpark, and
# Step-3 self-calibration from real 429 headers corrects the aggregate). NOT a billing figure and NOT the ceiling.


def _est_call_tokens(prompt, system, out_tokens):
    """Estimated INPUT+OUTPUT tokens for one call — the amount the dispatch TPM bucket debits so a fan can't blow a
    provider's tokens/minute limit (the real 429 axis). INPUT via a cheap chars→tokens heuristic (no tokenizer on the
    hot path — a soft rate bucket wants a ballpark, not a billing count); OUTPUT = the caller's max_tokens when pinned,
    else a nominal allowance. Deliberately arithmetic (a bound on a real quantity), never a meaning judgement."""
    n_in = (len(prompt) if isinstance(prompt, str) else len(str(prompt or ""))) // 4
    n_sys = (len(system) // 4) if system else 0
    n_out = int(out_tokens) if out_tokens else _NOMINAL_OUT_TOKENS
    return n_in + n_sys + max(0, n_out)


def _call_once(model, prompt, max_tokens=None, system=None, reasoning=None, schema=None, timeout_s=None,
               _skip_lane=False, no_metered_fallback=False, images=None, _no_sub=False):
    """One raw request. Everything public goes through `call`, which adds the input and output guards.

    NO DEFAULT CAP. This carried `max_tokens=512` — the last place a number nobody chose could still reach a
    provider. _call_guarded always resolves a real budget before calling here, so a None arriving at this
    function means someone reached the raw path directly and never picked a budget; that is refused rather
    than silently given the smallest cap in the codebase."""
    if max_tokens is None:
        raise ValueError(
            "_call_once needs an explicit max_tokens. It is the raw path — the measured budget lives in "
            "call()/_call_guarded, so use those unless you have deliberately chosen a number. A default "
            "here is how a cap nobody picked ends up truncating a real answer.")
    prov = provider_for(model)
    raw = model.split(":", 1)[1] if ":" in model else model
    spec = PROVIDERS[prov]
    # `finish_reason` is carried so callers can tell a COMPLETE answer from a TRUNCATED one.
    # Without it a caller inspecting r["text"] cannot distinguish "the model said this" from
    # "the model was cut off mid-sentence", and a truncated body that parses to nothing reads
    # as a member with no findings. It is a declared field on every provider's response, so
    # surfacing it is parsing, not inference.
    base = {"provider": prov, "model": raw, "text": None, "in_tok": 0, "out_tok": 0, "latency": 0.0, "cost": None, "finish_reason": None}
    # SUBSCRIPTION LANES (advisor.executor = claude-code | codex | pool): spendguard's own meta prompts
    # ride the matching flat-fee plan — $0 on the billed axis (recorded kind='subscription'); plan VALUE
    # is counted by the matching est-value pipeline (claude-code / codex session logs). Needs NO API key.
    # Any lane failure cools that lane and falls back to the caged API path below — degrade, never break.
    # VISION rides the metered API, never a subscription lane — the lane executors are text-only print-mode CLIs
    # (agy/codex/claude-code) with no image channel, so an images= call skips the lane by construction.
    _lane = None if (_skip_lane or images) else _lane_for(prov)
    if _lane and (_lane_too_big(_lane[0], prompt) or _lane_model_cooling(_lane[0], raw)):
        _lane = None                                 # prompt too big for this lane, OR this MODEL was rejected here
        #                                              recently (the API served it) → straight to API, don't re-intercept
    # CAPABILITY-AWARE AUTO-ROUTE (#1): a STRICT schema — one that DECLARES required/nonempty — cannot be reliably
    # produced by a prompt-only LANE. A lane is a print-mode CLI with NO response_format/forced-tool param, so the shape
    # only rides the prompt and the CLI wraps its JSON in prose → output_contract rejects it → the call churns to the
    # metered API REACTIVELY (the honestreview panel's measured backoffs). The vendor's METERED path is strictly more
    # capable for shape than its CLI lane — anthropic forced-tool / openai strict ENFORCE it; every compat vendor's
    # metered API runs response_format=json_object, which GUARANTEES parseable JSON (the CLI cannot) with the schema in
    # the prompt + local validate. So for a strict schema on a lane, skip the lane and use the vendor's meter: the
    # vendor is preserved, only lane→meter changes, and churn is removed by construction (Ash 2026-09-26: "lane cannot
    # satisfy but a meter can → straight to meter" — and the json_object meter CAN where the prose-wrapping CLI cannot).
    # A LENIENT schema (no required/nonempty) STAYS on the lane (a lane CAN satisfy it, with the lane-down failover as
    # the net) — so bulk $0 comprehension is untouched; only strict-schema calls pay the meter. Deterministic (no
    # cost/quality judgement), so a once-per-vendor notice makes it auditable without a per-call ledger row.
    if _lane and schema is not None:
        from . import output_contract as _oc_cap
        if _oc_cap.needs_enforcement(schema):
            _cap = "strict" if schema_capability(prov) == SCHEMA_STRICT else "json_object"
            config.warn_once(f"[spendguard] strict schema on {prov}: routing lane→metered {_cap} path (a prompt-only "
                             f"CLI lane cannot reliably return required/nonempty JSON) — #1 capability-aware auto-route")
            _lane = None                             # → the vendor's metered path below (strict, or json_object for compat)
    if _lane is None and prov == "gemini":           # METERED namespace: effort is a PARAMETER, not an id suffix.
        _bare, _tier = metered_fallback_id(prov, raw)  # an agy id (…-medium) 404s on the metered API — the ONE
        if _tier:                                    # equivalence fn splits it so the bare id rides the request and
            raw, base["model"], reasoning = _bare, _bare, (reasoning or _tier)   # the tier rides `reasoning` below
    if _lane:
        lane_name, lane_mod = _lane
        # THE SHAPE MUST RIDE THE PROMPT ON A LANE, AND THE DEADLINE MUST BE THE CALLER'S. A CLI completion
        # takes no response_format / forced-tool parameter, so a caller's `schema` was silently dropped here —
        # the model returned prose, output_contract rejected it, and the lane looked "unreliable" when it was
        # simply never told what to emit. Fold the shape into the system prompt (the same channel the
        # OpenAI-compatible path uses at line ~299) and let output_contract validate the text. And hand the
        # lane the caller's timeout_s, not its own 300s default, so a lane call is bounded exactly like the API
        # call it stands in for (a hung CLI must not outlive the budget the panel enforces on every vendor).
        _lane_sys = system
        if schema is not None:
            _shape = json_schema_request("compat", schema).get("_schema_prompt")
            if _shape:
                _lane_sys = ((system + "\n\n") if system else "") + _shape
        # Floor the lane deadline so a slow CLI/plan call is not timed out by a metered call's tight budget and
        # churned to the paid API; cap it at the lane's own TIMEOUT_S so a hung lane is still bounded. The floor is
        # PER-LANE (lane_mod.MIN_TIMEOUT_S, else the global): a codex COLD start needs ~75s+ (keep the generous
        # global floor), but a lane whose real latency is a few seconds (agy ~5s) declares a SHORT floor so a FLAKY
        # lane fails fast and degrades to the metered API in seconds, not 150s — the user gets a timely answer, and
        # the miss then cools the lane so subsequent calls skip it. The lane is $0, so the wait costs latency, not money.
        _lane_cap = int(getattr(lane_mod, "TIMEOUT_S", 300))
        _lane_floor = int(getattr(lane_mod, "MIN_TIMEOUT_S", LANE_MIN_TIMEOUT_S))
        _lane_timeout = min(_lane_cap, max(int(timeout_s or 0), _lane_floor))
        # AGY namespace: effort rides the MODEL-ID SUFFIX, and agy ignores the reasoning kwarg. So a bare id +
        # reasoning=medium must be respelled gemini-…-flash-medium here, or the tier is silently dropped and the lane
        # runs its default. Non-gemini lanes take the id unchanged.
        _lane_model = _compose_gemini_reasoning(raw, reasoning) if prov == "gemini" else raw

        def _call_lane():
            try:
                _s = lane_mod.run_prompt(prompt, system=_lane_sys, model=_lane_model, timeout=_lane_timeout,
                                         reasoning=reasoning, max_tokens=max_tokens)   # hand the lane the guarded budget
                #                                                                        so the escalation ladder can RAISE its cap
            except Exception as _le:                   # a lane MUST return an {error} dict, never raise — but a lane
                from . import gate as _glane
                if _glane.is_deliberate_stop(_le):     # a DELIBERATE stop (deadline / refusal / containment) HALTS —
                    raise                              # never swallowed into an error dict and then retried below
                _s = {"error": f"{lane_name} lane raised: {str(_le)[:120]}"}   # a genuine lane bug degrades, not crash
            if not isinstance(_s, dict):               # a non-dict is also a broken contract → treat it as an error
                _s = {"error": f"{lane_name} lane returned {type(_s).__name__}, not a result dict"}
            return _s
        s = _call_lane()
        # ON-LANE TRANSIENT RETRY before spilling to PAID metered: a lane that has PROVEN it can answer a prompt this
        # SIZE (a watermark FACT, never the error string) but failed THIS time is likely a momentary blip (plan
        # throttle / at-capacity) — retry the $0 lane ONCE after a short backoff, keeping the work FREE. Skipped for a
        # lane with no proven-good history (an ambiguous first miss → fast degrade to API) or one already cooling.
        # FACT-based, so no error-prose parsing (the doctrine); a genuine miss simply fails the retry and falls back.
        if (s.get("error") or not (s.get("text") or "").strip()) \
                and resource_state.proven_good(resource_state.lane_key(lane_name)) >= len(prompt or "") \
                and not _lane_cooling(lane_name):
            time.sleep(_LANE_RETRY_BACKOFF_S)
            _s2 = _call_lane()
            if not _s2.get("error") and (_s2.get("text") or "").strip():
                s = _s2                                # the retry answered — stay on the $0 lane, no metered spill
        # A "$0 plan reply" is only a real answer if it has CONTENT and — when a shape was requested — SATISFIES it.
        # WHITESPACE-only counts as empty; a non-empty reply that fails the requested schema is a lane MISS, not a
        # success. Either way, name it an error so the call FALLS BACK to the metered API instead of handing the
        # caller a blank/off-shape chunk. (This is the gap behind "the chunks that worked were the ones on the API":
        # an empty/whitespace/off-shape lane reply was recorded as a $0 success and returned, never failing over.)
        _txt = (s.get("text") or "").strip()
        _shape_ok = True
        if _txt and schema is not None:
            try:
                from . import output_contract
                _shape_ok = bool(output_contract.check_item(_txt, schema)[0])   # same validator the caller uses
            except Exception:
                _shape_ok = True                       # a validator that ITSELF errors is not the lane's fault → keep
        if not s.get("error") and _txt and _shape_ok:  # SUCCESS: no error, real content, AND (if asked) the shape
            _lane_note_ok(lane_name, prompt)         # proven-good watermark: this lane answered a prompt this big
            if lane_name not in _lane_echoed:        # tell the user ONCE per lane per run that a plan is serving their work
                _lane_echoed.add(lane_name)
                import sys as _syse
                print(f"[spendguard] 🛣  {lane_name} plan is serving {raw} prompts this run ($0 billed, not the metered API)",
                      file=_syse.stderr)
            # RECORD THE APPLIED EFFORT, NOT THE REQUESTED TIER. A lane CLI may map the request to its own scale
            # (codex 'minimal'→'none'); recording the request would mislabel what RAN and make a lane call look like
            # it under- or over-reasoned vs its metered twin when it did not. The executor reports the effort it
            # actually applied (`s['effort']`); fall back to the requested tier only for a lane that reports none.
            _applied_eff = s.get("effort") if s.get("effort") is not None else reasoning
            try:
                from . import calls
                calls.record_call(prov, raw, "subscription", 0.0,
                             in_tok=s.get("in_tok", 0), out_tok=s.get("out_tok", 0), latency=s.get("latency"),
                             executor=lane_name, effort=_applied_eff)  # the plan that served it + the effort it APPLIED
            except Exception:
                pass
            return {**base, "text": s["text"], "in_tok": s.get("in_tok", 0), "out_tok": s.get("out_tok", 0),
                    "latency": s.get("latency", 0.0), "cost": 0.0, "executor": lane_name,
                    "effort": _applied_eff, "error": None}
        # STRUCTURED miss reason — a CODE, not the free-form error string — so a consumer splits shed vs shape vs
        # empty vs quota without parsing prose (the lane twin of the vision runner's reason codes). The branch below
        # ALREADY knows the category; this only names it.
        if not s.get("error"):                         # no error but not a usable answer → say WHY (empty vs off-shape)
            _lane_reason = "empty" if not _txt else "shape_miss"
            s = {**s, "error": (f"{lane_name} lane returned no usable text (empty/whitespace)" if not _txt
                                else f"{lane_name} lane output did not satisfy the requested shape → API")}
        else:
            _lane_reason = "lane_error"                # the lane set its OWN error upstream (raised / non-dict / envelope)
        _ra = s.get("retry_after_s") if isinstance(s, dict) else None
        if _ra:
            _lane_reason = "quota"                     # a parsed quota/reset signal overrides the generic classification
            # A STRUCTURED quota/exhaustion signal the lane EXECUTOR parsed from its own CLI's envelope (a known
            # shape it owns — see antigravity_exec._reset_window_s). This is the one failure the API-outcome
            # doctrine below cannot see: a quota-limited lane's metered API twin answers fine, so the fallback would
            # otherwise read the miss as a SIZE limit and pin a ceiling. Demote it until its reset window — but
            # CAPPED (_max_quota_cool_s), because an oscillating quota (agy) must be re-tested, not bypassed for
            # days. `transient` then tells _learn_from_fallback this was quota, so it learns NO size ceiling.
            _lane_cool(lane_name, seconds=min(float(_ra), _max_quota_cool_s()), reason="quota")
        if no_metered_fallback and _lane_reason != "lane_error":
            # $0-ONLY caller (--refuse-billed): it opted out of paying metered to RETRY A TASK the free lane found
            # UNSUITABLE (empty / off-shape / too big / a transient quota it will reset from) — that stays a $0 miss,
            # never a surprise charge. But a LANE_ERROR is the executor reporting the lane could NOT serve AT ALL
            # (login/token expired, CLI crash, rejected model) — infrastructure DOWN, not an unsuitable task. Losing
            # work to it is the silent-empty the user hit, so a down lane still applies the ladder below (reroute to
            # another $0 lane first, then metered) — a result is never lost. This is the STRUCTURAL split (which branch
            # set _lane_reason), never a parse of the error prose (which the agentic remediation owns). Decided with Ash
            # 2026-09-26: "a down lane still fails over."
            return {**base, "text": None, "cost": None, "executor": lane_name, "reason": _lane_reason,   # MISS is an
                    "error": f"refused: would bill metered API ({s.get('error')})"}   # error row, NOT a paid retry — $0
        if _lane_reason == "lane_error":                 # the lane could not serve → tell the user ONCE (never a silent
            _surface_lane_down(lane_name, s.get("error"))  # empty); the ladder below reroutes + cools via _learn_from_fallback
        # LANE FAILED. REACTIVE FAILOVER (Part 2) FIRST: before paying the metered API, try a CONFIRMED substitute
        # PLAN for this intent — one hop, guarded against recursion. Routed through call() so the substitute resolves
        # its OWN budget and rides its OWN lane; if it answers, the primary lane is cooled (it failed) and the
        # substitution is recorded. Default OFF: no confirmed substitute → route_decision returns None → unchanged.
        # `_no_sub` (caller's no_substitution=True) pins the vendor: the requested model answers or errors, never a
        # silent swap — for calls where the vendor IS the measurement (a cross-vendor panel / adjudication).
        if not _no_sub and not getattr(_sub_guard, "on", False):
            try:
                from . import lane_balance, calls as _calls
                _rsub, _rwhy = lane_balance.route_decision((_calls.current() or {}).get("intent"), model, reactive=True)
            except Exception:
                _rsub, _rwhy = None, ""
            if _rsub and _rsub != model:
                import sys as _sysr
                print(f"[spendguard] lane-balance REACTIVE: {lane_name} lane failed → {_rsub} ({_rwhy})",
                      file=_sysr.stderr)
                _sub_guard.on = True
                try:
                    _rr = call(_rsub, prompt, max_tokens=max_tokens, system=system, reasoning=reasoning,
                               schema=schema, timeout_s=timeout_s)
                finally:
                    _sub_guard.on = False
                if not _rr.get("error"):
                    _lane_cool(lane_name, reason="failover")   # primary failed → back off; the substitute carried it
                    return {**_rr, "substituted_from": model, "substitution": _rwhy}
        # No substitute (or it also failed) → fall back to the API on the SAME prompt (this recurses with the lane
        # disabled, so the existing API path runs once, unchanged) and let its OUTCOME settle whether the lane was
        # unsuitable for this prompt (keep it) or genuinely down (cool it).
        # ATOMIC-PAIR FIDELITY (the reasoning-equivalence map): the lane just tried its realization of `reasoning`; its
        # SAME-provider metered twin must fall back at EQUAL-OR-GREATER reasoning and on the SERVED equal-model id —
        # never UNDER-reason (an agy/Gemini 'minimal' runs the lane's default tier, so its metered fallback must match,
        # not drop to 'none') and never strand on a stale alias (claude-haiku-4-5 → the served dated id). resolve() is
        # $0 (cached) and wrapped so a map hiccup degrades to the prior behaviour, never breaks the fallback.
        _fb_model, _fb_reasoning = model, reasoning
        try:
            from . import reasoning_equivalence as _re, lane_catalog as _lc
            _base_m, _suf_lv = _lc.parse_use_name(raw, lane_name)      # split any agy reasoning suffix off the id
            _cell = _re.resolve_metered(lane_name, _base_m, reasoning or _suf_lv)
            _fb_reasoning = _cell["metered_effort"]                    # EQUAL, proven-lesser, or rounded-up (never less)
            if _cell.get("metered_model"):
                _fb_model = f"{prov}:{_cell['metered_model']}"         # the SERVED equal-model id (resolves stale alias)
        except Exception:
            _fb_model, _fb_reasoning = model, reasoning                # map unavailable → prior behaviour, never break
        out = _call_once(_fb_model, prompt, max_tokens=max_tokens, system=system, reasoning=_fb_reasoning,
                         schema=schema, timeout_s=timeout_s, _skip_lane=True, _no_sub=_no_sub)
        _kind = _learn_from_fallback(lane_name, prompt, bool(out.get("error")), model=raw, transient=bool(_ra))
        import sys as _sys
        if _kind == "transient":                       # quota/rate — cooled until reset (capped), re-tested; NOT size
            _cool = int(min(float(_ra), _max_quota_cool_s()))
            print(f"[spendguard] {lane_name} lane hit a quota/rate limit — cooled {_cool}s then re-tested (NOT a "
                  f"size limit); the API served this call ({str(s['error'])[:60]})", file=_sys.stderr)
        elif _kind == "unsuitable":                    # a genuine size limit ABOVE the lane's proven-good size
            _ceil = resource_state.size_ceiling(resource_state.lane_key(lane_name))
            print(f"[spendguard] {lane_name} lane unsuitable above its proven-good size — prompts >= {_ceil} chars "
                  f"now route to API ({str(s['error'])[:60]})", file=_sys.stderr)
        elif _kind == "model-cooled":                  # ambiguous miss (schema/content) — back off THIS model briefly
            print(f"[spendguard] {lane_name} lane missed this prompt ({raw}, within its handled size) — backing "
                  f"off that model briefly; the API answered it ({str(s['error'])[:60]})", file=_sys.stderr)
        else:                                          # "down" — API also failed
            print(f"[spendguard] {lane_name} lane unavailable — cooling {int(_pool_cooldown_s())}s; the API "
                  f"fallback also failed ({str(s['error'])[:60]})", file=_sys.stderr)
        return out
    key = config.api_key(spec["key_env"])
    if not key:
        return {**base, "error": f"no key ({spec['key_env']})"}
    # LIVE-CATALOG PRE-FLIGHT (grounding): a metered id remembered from a past session may be dead now — Gemini
    # rotates ids, and a dead id today surfaces as a mystery provider 404. This mirrors the input-window pre-flight
    # in vendor_call.call (fail here, naming the two facts, instead of paying for a provider rejection). It reuses
    # the served-list primitive vendor_call.served_check (cache-first, $0, no per-call latency; a would-be
    # rejection is CONFIRMED LIVE via serves() so a stale cache can't false-reject) at THIS single choke point, so
    # every vendor/lane path inherits it (vendor_call.call routes through here too). Only a CONFIRMED-stale id is
    # refused — with the currently-served SAME model named agentically (closest_served → pricing._same_model_as_ours,
    # a meaning call, never string distance). 'unchecked' (no synced list, or discovery unavailable) proceeds: a
    # can't-check is never a rejection. `raw` is already respelled to the bare metered id for gemini above.
    try:
        from . import vendor_call as _vc, catalog as _catalog
    except Exception:
        _vc = None                                     # packaging edge — nothing to validate against; skip silently
    if _vc is not None:
        try:
            _status = _vc.served_check(prov, raw)
            if _status == "stale":
                _same, _live = _vc.closest_served(prov, raw)
                _hint = (f" — did you mean {_same!r} (the currently-served same model)?" if _same
                         else (f" — {len(_live)} models are currently served; `spendguard sync-catalog` lists them"
                               if _live else ""))
                return {**base, "executor": "api", "error_type": "StaleModelId",
                        "error": f"{prov} does not serve {raw!r} (not in its live /models catalog){_hint}."}
            if _status == "unchecked" and _catalog.note_unknown_once(prov):
                import sys as _syscat
                print(f"[spendguard] served-list not synced for {prov} — cannot verify {raw!r} is live; proceeding. "
                      f"Run `spendguard sync-catalog` to catch stale ids at dispatch.", file=_syscat.stderr)
        except Exception as _ce:
            # The PRE-FLIGHT ITSELF failed (a bug, or a corrupted cache) — fail OPEN so a broken check never blocks a
            # real call, but make it VISIBLE once per provider so it can't silently disable the stale-id protection
            # forever (the swallowed-exception blind spot). NOT a stale verdict: a check we couldn't run is unchecked.
            try:
                if _catalog.note_unknown_once(prov):
                    import sys as _scv
                    print(f"[spendguard] served-list check errored for {prov} ({str(_ce)[:80]}) — proceeding "
                          f"UNVALIDATED; a stale id would not be caught. Fix the catalog cache / `sync-catalog`.",
                          file=_scv.stderr)
            except Exception:
                pass                                   # even the warning path must not break the dispatch
    t0 = time.time()
    _cache_read = _cache_write = 0        # prompt-cache usage (read / write tokens), captured per provider below
    _effort_sent = None                   # the reasoning_effort ACTUALLY sent — OpenAI-compat only (set in that branch);
    #                                       anthropic uses a thinking BUDGET, not this param, so it stays None here.
    _eff_requested = None                  # the reasoning tier the CALLER asked for (for the per-call guardrail-A surface)
    _eff_unhonored = False                 # True when that pin could not be honored (minimal→a still-reasoning floor)
    try:
        if spec["kind"] == "anthropic":
            import anthropic
            # A CLIENT-SIDE TIMEOUT IS THE ONLY THING THAT ACTUALLY CANCELS A REQUEST. A caller that merely
            # stops waiting (thread.join(timeout=...)) leaves the request running to completion, and it BILLS:
            # measured, a review that saw 18 results was billed for 146 calls, because every abandoned call
            # finished on its own and the caller never learned it existed. Handing the SDK the same budget the
            # caller is enforcing makes abandonment real.
            # timeout: fast-connect / full-read budget (see _http_timeout). max_retries=0: vendor_call owns the
            # retry loop, and the SDK's default of 2 would silently TRIPLE a down vendor's wall time inside a
            # single attempt — invisible to the caller's deadline, which is how the 3h30m run stayed hidden.
            c = (anthropic.Anthropic(api_key=key, timeout=_http_timeout(timeout_s), max_retries=0)
                 if timeout_s else anthropic.Anthropic(api_key=key))
            _uc = ([{"type": "text", "text": prompt}] + _image_parts(images, "anthropic")) if images else prompt
            kw = {"model": raw, "max_tokens": max_tokens, "messages": [{"role": "user", "content": _uc}]}
            if system:
                # CACHE the long, stable system prompt: it bills once (a cache WRITE) then reads at a discount on every
                # repeat — the biggest win for a caller sending an identical *_SYSTEM on each call (honestreview's
                # write-time judges). Only when it clears the cache minimum (a real-quantity token bound → deterministic,
                # not a magic judgement); otherwise a plain string. Fail-open: a cached-shape rejection retries plain
                # at the call site below, so a caching hiccup can never break the call.
                kw["system"] = ([{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}]
                                if _cacheable_system(system) else system)
            # EXTENDED THINKING (a token BUDGET) from a MEASURED fact — models.thinking_budget, never a guess.
            # It CONFLICTS with a forced-tool schema on anthropic, so it is applied ONLY when schema is None; when
            # both are asked for, thinking is suppressed with a one-time notice (never a silent 400). No fact → no
            # thinking (the honest 'default until measured' behaviour). Fail-open below strips it on any rejection.
            from . import models as _mdl
            _tb = _mdl.thinking_budget(raw, reasoning, max_tokens) if reasoning else None
            if schema is not None:
                kw.update(json_schema_request("anthropic", schema))
                if _tb:
                    config.warn_once("[spendguard] anthropic extended thinking is suppressed under a forced-tool "
                                     "schema (they conflict) — call %r WITHOUT schema to use thinking" % raw)
            elif _tb:
                kw["thinking"] = {"type": "enabled", "budget_tokens": _tb}
            # STREAM, always. The SDK REFUSES a non-streaming request whose max_tokens implies a run over ten
            # minutes ("Streaming is required for operations that may take longer than 10 minutes"), and the
            # threshold is the SDK's, not ours — guessing it would be a magic number that silently rots when
            # they change it. Streaming has no such bound, returns the identical final message and usage, and
            # measured 20/20 with no latency penalty. So the cap can be sized from measured need, which is the
            # whole point of a termination bound, without the transport vetoing it.
            # WALL-CLOCK BOUND (same rationale as the OpenAI-compat path below): the SDK's httpx timeout is
            # PER-READ, so a slow/long streamed body can outlive timeout_s while each chunk arrives in the window.
            # Run the stream on a daemon worker, join at timeout_s, and on timeout CLOSE the client (tears down the
            # connection → cancels the request, stops billing) and raise _CallDeadline. In-time → the full message,
            # so usage/cost stay EXACT. No timeout_s → the plain stream (unchanged).
            def _anth_msg(_kw):                               # one final message; timeout_s bounds it at wall-clock
                if timeout_s:
                    _abox = {}
                    from . import calls as _actx
                    _apctx = dict(_actx.current() or {})      # CARRY the thread-local intent/chain + caller across the
                    _acaller = _actx.caller()                 #   daemon boundary: the gated SDK records this call FROM
                    #   the daemon below, whose context is EMPTY and whose stack is threading.py:run — so without this
                    #   the metered row lands intent=None + caller=threading.py:run (the failure vendor_call._attempt
                    #   documents). caller() is resolved HERE, on the worker's real stack, then set inside the daemon.
                    def _astream():
                        try:
                            _actx.set_context(intent=_apctx.get("intent"), chain=_apctx.get("chain"),
                                              who=_apctx.get("who") or _acaller)
                        except Exception:
                            pass
                        try:
                            with c.messages.stream(**_kw) as _s:
                                _abox["m"] = _s.get_final_message()
                        except BaseException as _be:          # capture ALL (incl. the close-induced error); caller decides
                            _abox["e"] = _be
                    _ath = threading.Thread(target=_astream, daemon=True)   # daemon: an abandoned hung stream must not hold the process
                    _ath.start()
                    _ath.join(float(timeout_s))
                    if _ath.is_alive():
                        try:
                            c.close()                        # cancel the in-flight request (best-effort billing stop)
                        except Exception:
                            pass
                        try:
                            from . import bulkgate as _bg_dc   # #3: cut mid-generation → invisible waste (see OpenAI path)
                            _bg_dc.note_deadline_cancel(model, timeout_s)
                        except (ImportError, AttributeError):
                            pass                             # note_deadline_cancel itself never raises; guard only the import
                        raise _CallDeadline("deadline_exceeded: no completion within %.0fs (wall-clock)" % float(timeout_s))
                    if "e" in _abox:
                        raise _abox["e"]
                    return _abox["m"]
                with c.messages.stream(**_kw) as s:
                    return s.get_final_message()
            try:
                m = _anth_msg(kw)
            except _CallDeadline:
                raise                                         # a wall-clock deadline HALTS — never a caching retry
            except Exception:
                # FAIL-OPEN once: strip the two OPTIONAL enrichments a model/endpoint might reject — a cached system
                # BLOCK (→ plain string) and the THINKING budget (→ drop it) — so a caching or thinking hiccup can
                # never break the call. If neither was set, there is nothing to relax → re-raise the real error.
                _retry, _relaxed = dict(kw), False
                if isinstance(_retry.get("system"), list):
                    _retry["system"] = system; _relaxed = True
                if "thinking" in _retry:
                    _retry.pop("thinking"); _relaxed = True
                if _relaxed:
                    m = _anth_msg(_retry)
                else:
                    raise
            # With a forced tool the answer arrives as tool_use.input, not as text — reading only text blocks
            # would return "" and look exactly like the empty-response failure.
            tu = [b for b in m.content if getattr(b, "type", None) == "tool_use"]
            text = (json.dumps(tu[0].input) if tu
                    else "".join(b.text for b in m.content if getattr(b, "type", None) == "text"))
            # Anthropic reports cache usage SEPARATELY: input_tokens is the FRESH (uncached) input; a cache READ and a
            # cache WRITE are their own counts. Total read-side input = fresh + cache_read (the write is priced apart).
            _cache_read = int(getattr(m.usage, "cache_read_input_tokens", 0) or 0)
            _cache_write = int(getattr(m.usage, "cache_creation_input_tokens", 0) or 0)
            in_tok, out_tok = int(m.usage.input_tokens) + _cache_read, int(m.usage.output_tokens)
            # THE FIELD WAS DECLARED AND NEVER SET. `base` has carried "finish_reason": None since this
            # function was written, and the comment above says callers use it to tell a complete answer from
            # a truncated one — but no provider branch ever assigned it, so it was None on every reply and
            # every caller checking it learned nothing. Anthropic calls it stop_reason.
            _finish = getattr(m, "stop_reason", None)
        else:
            from openai import OpenAI
            # Same fast-connect / no-SDK-retry discipline as the anthropic branch: a blackholed OpenAI-compatible
            # endpoint (z.ai, moonshot) must fail on CONNECT in ~10s, not hold the slot for the whole read budget.
            c = (OpenAI(api_key=key, base_url=spec["base_url"], timeout=_http_timeout(timeout_s), max_retries=0)
                 if timeout_s else OpenAI(api_key=key, base_url=spec["base_url"]))
            _uc = ([{"type": "text", "text": prompt}] + _image_parts(images, "openai")) if images else prompt
            msgs = ([{"role": "system", "content": system}] if system else []) + [{"role": "user", "content": _uc}]
            okw = {"model": raw, "messages": msgs}
            if schema is not None:
                try:
                    sk = json_schema_request(_schema_kind(prov), schema)   # SSOT: openai→strict, other compat→json_object
                except SchemaNotStrictExpressible as _se:   # a map schema strict mode would empty to {} → refuse LOUD
                    return {**base, "error": str(_se), "error_type": "SchemaNotStrictExpressible", "cost": None}
                # `_schema_prompt` is OURS, not the provider's — it carries the shape that json_object mode
                # cannot express. Sending it as an API parameter would 400; it belongs in the system message,
                # which is the only channel a compat endpoint has for "which JSON".
                extra = sk.pop("_schema_prompt", None)
                if extra:
                    msgs = ([{"role": "system", "content": ((system + "\n\n") if system else "") + extra}]
                            + [m for m in msgs if m.get("role") != "system"])
                okw["messages"] = msgs
                okw.update(sk)
            # REASONING IS BILLED AS OUTPUT, AND IT IS MOST OF THE BILL. Measured over a 4-vendor code review:
            # 91% of the cost was output, and 92-98% of that output was reasoning nobody ever sees —
            # glm-5.2 emitted 11,176 tokens per call to deliver 262 tokens of findings.
            #
            # This used to be gated on `re.match(r"(gpt-5|o[134])", raw)`: a hardcoded list of OpenAI's own
            # model names. So the control reached the models that needed it LEAST and never reached kimi-k3
            # or glm-5.2, which reason the most. Both accept the parameter — measured directly, minimal cut
            # kimi-k3 from 316 output tokens to 92 (3.4x) and glm-5.2 from 898 to 60 (15x).
            #
            # It now goes to EVERY OpenAI-compatible endpoint. Nothing had to be guessed about which models
            # support it, because the retry below already drops the parameter for any endpoint that refuses —
            # the guard was protecting against a case that was already handled, at the cost of the saving.
            # NO DEFAULT. Sending "minimal" to everything was a hand-picked bound wearing a cost saving,
            # and MEASURED it destroys the work: glm-5.2 reviewing calls.py at minimal returned 10 output
            # tokens and ZERO findings, where the same file at high returned 793 tokens and correctly found
            # the naive-local-timestamp bug. The "96x cheaper" that came out of the first measurement was
            # 96x cheaper because it had stopped reviewing.
            #
            # It is not even monotonic: kimi-k3 found MORE at minimal on pricing.py (3) than at high (2).
            # Effort is a property of (call-class, model) and only measurement can settle it — the same rule
            # already applied to max_tokens and to the deadline, and the only one of the three still being
            # set by hand.
            #
            # Until a class has a measured effort, send NOTHING and let the vendor use its own default. An
            # invented bound is worse than no bound: no bound is at least honest about being unmeasured.
            # THE VERIFIED FACT MUST BIND, or it is a note in a file. models.py exists to be "the single
            # place that knows each model family's quirks and APPLIES them", and its docstring names this
            # exact failure: "'gpt-5 wants reasoning=none' sitting in memory while a call burns its whole
            # budget on reasoning and returns empty". Measured: gpt-5.5 returned EMPTY on 2 of 53 validation
            # calls for precisely that reason, while models.py had carried reasoning='none' for it all along.
            #
            # This is not an invented default. It is a RECORDED value with provenance — either a verified
            # family fact or one an A/B wrote via models.add_fact() (kimi-k3='auto', glm-5.2='high' came
            # from a measured run). An explicit caller argument still wins over both.
            #
            # THE LOOKUP ITSELF LIVES IN models.apply_call_params AND NOWHERE ELSE. This block used to carry
            # its own copy of it, which is how a fact store ends up applying to some calls and not others.
            # The dialect is passed explicitly because this branch KNOWS it is speaking Chat Completions,
            # while the model name alone does not: kimi-k3 and glm-5.2 match no family rule, so inferring the
            # shape from the name would answer '?' and drop their measured facts on the floor.
            if reasoning:
                try:                                       # STANDARD ordinal (minimal|low|medium|high) → this model's
                    from . import models as _mfn           # VERIFIED reasoning_effort value (only the floor varies per model)
                    _eff = _mfn.normalize_reasoning(raw, reasoning)
                except Exception:
                    _eff = reasoning
                if _eff is not None:
                    okw["reasoning_effort"] = _eff         # explicit caller argument: applied, then respected
                    # GUARDRAIL A — HONOR OR REFUSE, NEVER SILENTLY DROP AN EXPLICIT EFFORT PIN. A caller's 'minimal' is
                    # a COST pin; on a model whose verified floor is not 'minimal' (gpt-5.x floor='none') it is remapped
                    # to that floor, and if the model STILL reasons at the floor (reasons_by_default) the pin buys NO
                    # saving — the model burns thousands of reasoning tokens exactly as if unpinned (measured: gpt-5.5
                    # 'none' → ~4,249 out tok / $0.13 vs gpt-5-mini honored 'minimal' → 121 tok, the $45 warden
                    # overspend). A control the caller set that silently does nothing reads as safe and is not, so SAY
                    # SO loudly and record it (docs/GUARDRAILS_reasoning_overspend.md §A). Never guesses a value — only
                    # surfaces facts models.py already holds (the floor + reasons_by_default) plus the caller's pin.
                    _eff_requested = reasoning     # what the caller asked for — surfaced per-call so spendrails/warden can read it
                    try:
                        from . import models as _mA, bulkgate as _bgeff
                        if str(reasoning).strip().lower() == "minimal" and _eff != "minimal" \
                                and _mA.reasons_by_default(raw):
                            _eff_unhonored = True    # the pin could not be honored (per-call flag → a caller can abort on it)
                            _bgeff.note_unhonored_effort(raw, "minimal", _eff)   # records first, then best-effort warns;
                            #                                                      never raises, so the trace is reliable
                    except Exception as _eA:
                        # Advisory-only — the call proceeds (guardrail D's cap + the ledger's real-spend record for
                        # reconcile are the load-bearing controls) — but NEVER silent: a guardrail that vanished without
                        # a trace is the very anti-pattern this fixes, so say the SURFACE degraded, loudly (itself
                        # guarded so a broken stderr can't turn a degraded surface into a broken call).
                        try:
                            import sys as _sA
                            print("[spendguard] guardrail-A effort-honor check errored (%s) on %s — the call proceeds; "
                                  "confirm the '%s' pin via the ledger" % (type(_eA).__name__, raw, reasoning),
                                  file=_sA.stderr)
                        except Exception:
                            pass
            try:
                from . import models as _mf
                _mf.apply_call_params(raw, okw, dialect="openai")
            except Exception:
                pass                                        # a missing fact store must not break the call
            # WALL-CLOCK BOUND for this OpenAI-compat generation. The SDK's httpx timeout is PER-READ, so a long or
            # slow high-reasoning body (kimi-k3 on moonshot, glm on z.ai) trickles under the read window while total
            # elapsed sails past the deadline — wedging the caller's queue with a call `timeout_s` never bounded. Run
            # each completion on a daemon worker, join at timeout_s, and on timeout CLOSE the client (tears down the
            # connection → cancels the request, stops further billing) and raise _CallDeadline. Finishes in time →
            # the full completion, so usage/cost stay EXACT (no streaming, no token-count guess). Every create() in
            # the ladder below goes through this, so no provider call on this path can outlive the caller's deadline.
            # SUCCESS-HEADER LEARNING: read the vendor's rate-limit headers off the SUCCESSFUL response (they ride
            # x-ratelimit-limit-* on every response, not only the 429) so the governor paces it BEFORE it ever 429s.
            # with_raw_response gives the headers + a .parse() to the same completion; a client without it (or a compat
            # endpoint that omits the headers) transparently falls back to the plain create, and learning is a no-op.
            _wraw = getattr(c.chat.completions, "with_raw_response", None)
            _raw_create = _wraw.create if _wraw is not None else c.chat.completions.create
            def _finish_create(_res):
                if _wraw is None:
                    return _res                          # plain create already returns the PARSED completion
                _learn_success_limits(prov, _res)        # _res is the RAW wrapper → learn its headers, then parse
                return _res.parse()
            def _bounded_create(**cw):
                if not timeout_s:
                    return _finish_create(_raw_create(**cw))
                _box = {}
                from . import calls as _octx
                _opctx = dict(_octx.current() or {})          # CARRY intent/chain + caller across the daemon boundary
                _ocaller = _octx.caller()                     #   (see the anthropic branch): the gated SDK / http_capture
                #   records this call FROM the daemon, whose context is EMPTY and whose stack is threading.py:run —
                #   without this the metered row lands intent=None + caller=threading.py:run. caller() resolved HERE.
                def _worker():
                    try:
                        _octx.set_context(intent=_opctx.get("intent"), chain=_opctx.get("chain"),
                                          who=_opctx.get("who") or _ocaller)
                    except Exception:
                        pass
                    try:
                        _box["r"] = _raw_create(**cw)
                    except BaseException as _be:              # capture ALL (incl. a close-induced error); main thread decides
                        _box["e"] = _be
                _th = threading.Thread(target=_worker, daemon=True)   # daemon: an abandoned hung call must not hold the process
                _th.start()
                _th.join(float(timeout_s))
                if _th.is_alive():
                    try:
                        c.close()                            # cancel the in-flight request (best-effort billing stop)
                    except Exception:
                        pass
                    try:
                        from . import bulkgate as _bg_dc     # #3: cut MID-generation → provider bills what it generated
                        _bg_dc.note_deadline_cancel(model, timeout_s)   # (reasoning tokens → nothing), invisible to the
                    except (ImportError, AttributeError):     # local ledger. Surface it; note_deadline_cancel never raises.
                        pass
                    raise _CallDeadline("deadline_exceeded: no completion within %.0fs (wall-clock)" % float(timeout_s))
                if "e" in _box:
                    raise _box["e"]                          # a real error (e.g. a param 400) → the ladder handles it
                return _finish_create(_box["r"])
            try:                                              # gpt-5+ require max_completion_tokens; older models take max_tokens
                r = _bounded_create(max_completion_tokens=max_tokens, **okw)
            except Exception as e:
                # WHICH PARAMETER, FROM THE TYPED FIELD — not from the message text. These branches matched
                # `"response_format" in str(e)` and `"reasoning_effort" in str(e)`, so an error that merely
                # MENTIONED a parameter (a validation error listing the whole request, a message quoting the
                # body back) took the retry path and silently dropped a parameter the caller asked for —
                # and the schema being dropped is precisely how "required" fields come back as zeros.
                from . import models as _mp, gate as _og
                if isinstance(e, _CallDeadline):
                    raise                                   # a WALL-CLOCK deadline HALTS — it is a timeout, not a param to drop
                if isinstance(e, _og.deliberate_stop_types()):
                    raise                                   # a spend refusal / deadline must NOT enter the retry ladder
                _bad = _mp._rejected_param(e)
                # THE LADDER. Each rung removes ONE optional thing and retries. Which rung to take comes
                # from the provider's typed `param` when it supplies one; when it supplies none there is
                # nothing to read, so every applicable rung is tried in a fixed order.
                #
                # What this replaces: four branches that each asked whether a phrase appeared in the
                # error MESSAGE. That is a judgement about free text, and it was wrong in both directions
                # — an error merely quoting the request body back took a retry path and silently dropped a
                # parameter the caller asked for (a dropped schema is how "required" fields return as
                # zeros), while a rejection worded differently fell through to `raise`. Trying the rungs is
                # not a guess about meaning: it is bounded, it is recorded, and the outcome of the retry —
                # not the wording of a sentence — decides what was actually wrong.
                _RUNGS = ("response_format", "reasoning_effort", "_token_dialect", "_token_budget")
                if _bad in ("response_format", "json_schema"):
                    _ladder = ("response_format",)
                elif _bad == "reasoning_effort":
                    _ladder = ("reasoning_effort",)
                elif _bad in ("max_tokens", "max_completion_tokens"):
                    # Either the wrong spelling for this endpoint, or a budget above what it allows. Try the
                    # spelling first (free), then walk the budget down.
                    _ladder = ("_token_dialect", "_token_budget")
                else:
                    _ladder = _RUNGS                     # unattributable: try each applicable rung
                r = None
                _last = e
                for _rung in _ladder:
                    if _rung == "_token_budget":
                        # THE FLOOR IS HIGH, AND SOME MODELS CANNOT TAKE IT. Sending 32K to a model whose
                        # maximum output is 8192 is a 400. Rather than hardcode a per-model ceiling — a
                        # provider fact I would be guessing at, and one that changes — halve until the
                        # provider accepts, then record what it accepted as a learned fact so the next call
                        # starts there. The provider is the source of truth about its own limits.
                        r = _heal_token_budget(
                            lambda b: _bounded_create(max_completion_tokens=b, **okw), max_tokens, raw)
                        if r is not None:
                            break                          # accepted at a plausible budget → learned + done
                        continue                           # nothing above the floor worked → try the next rung
                    if _rung == "_token_dialect":
                        # Older endpoints take max_tokens, gpt-5+ take max_completion_tokens. A dialect
                        # difference, not a capability one — nothing is dropped, the other spelling is used.
                        try:
                            r = _bounded_create(max_tokens=max_tokens, **okw)
                            break
                        except Exception as e2:
                            if isinstance(e2, _CallDeadline):
                                raise                        # a wall-clock deadline HALTS — do not walk to the next rung
                            _last = e2
                            continue
                    if _rung == "reasoning_effort" and "reasoning_effort" in okw and not getattr(_heal_guard, "on", False):
                        # HEAL before dropping: the endpoint rejected this effort, but the caller asked for a TIER —
                        # discover a value it accepts that preserves that tier (gpt-5.4-nano: 'minimal'→'none'),
                        # retry, and on success mark it VERIFIED so every later call (and normalize_reasoning, which
                        # a batch builder reads) sends the right value with no 400. Only if healing finds nothing do
                        # we fall through to the drop below (degrade to the model's default effort). Skipped while
                        # discover_efforts is probing (_heal_guard) — a probe must be DROPPED so it reads as rejected.
                        from . import models as _mh, gate as _hg
                        _pre = okw.get("reasoning_effort")
                        if _mh.heal_reasoning(raw, okw, e):
                            try:
                                r = _bounded_create(max_completion_tokens=max_tokens, **okw)
                                _mh.confirm_reasoning(raw, okw)      # the healed value WORKED → promote it to verified
                                import sys as _sh
                                print(f"[spendguard] {prov}/{raw} rejected reasoning_effort={_pre!r} → healed to "
                                      f"{okw.get('reasoning_effort')!r} (learned; kept the requested tier).", file=_sh.stderr)
                                break
                            except Exception as e2:
                                if isinstance(e2, _CallDeadline) or isinstance(e2, _hg.deliberate_stop_types()):
                                    raise                    # a wall-clock deadline / spend refusal HALTS — never a drop
                                okw["reasoning_effort"] = _pre   # heal's value also failed → restore, fall to the drop
                                _last = e2
                    if _rung not in okw:
                        continue                          # not sent, so it cannot be what was refused
                    _saved = okw.pop(_rung)
                    try:
                        r = _bounded_create(max_completion_tokens=max_tokens, **okw)
                        # SAY SO. A silently-dropped parameter makes the call succeed and makes any probe
                        # of "does this endpoint support X" answer yes — the fallback that keeps the system
                        # robust is exactly what blinds discovery. Recorded on the result so a caller can
                        # tell "it worked" from "it worked WITHOUT what you asked for".
                        base.setdefault("dropped", []).append(_rung)
                        # KEEP THE PROVIDER'S REASON ON THE RESULT, WHOLE. The stderr line below clips it to 70
                        # chars, which was exactly enough to read "Invalid schema for response_f…" and not the
                        # sentence after it that named the limit and the count. A caller diagnosing why its
                        # strict schema keeps being dropped needs the text the provider actually sent, not a
                        # prefix that stops before the diagnosis.
                        base.setdefault("dropped_why", {})[_rung] = str(e)
                        if _rung == "response_format":
                            import sys as _s
                            print(f"[spendguard] {prov}/{raw} rejected response_format ({str(e)[:70]}) — "
                                  f"sending unenforced; output_contract still validates (full reason on the "
                                  f"result under dropped_why).", file=_s.stderr)
                        break
                    except Exception as e2:
                        if isinstance(e2, _CallDeadline):
                            raise                        # a wall-clock deadline HALTS — do not try the next rung
                        okw[_rung] = _saved               # that rung was not the problem — put it back
                        _last = e2
                if r is None:
                    raise _last
            text = r.choices[0].message.content
            _effort_sent = okw.get("reasoning_effort")   # okw is final here (post heal/drop) — the effort truly sent
            in_tok, out_tok = r.usage.prompt_tokens, r.usage.completion_tokens
            try:                                              # OpenAI auto-caches identical prefixes ≥1024 tok; unlike
                _ptd = getattr(r.usage, "prompt_tokens_details", None)   # Anthropic, prompt_tokens ALREADY INCLUDES the
                _cache_read = int(getattr(_ptd, "cached_tokens", 0) or 0) if _ptd else 0   # cached read (no add), and there
            except Exception:                                 # is no separate write charge (_cache_write stays 0).
                _cache_read = 0
            _finish = getattr(r.choices[0], "finish_reason", None)     # "length" when it hit the cap
        dt = time.time() - t0
        try:
            cost = pricing.realtime_cost(raw, in_tok, out_tok, cached_in_tok=_cache_read, cache_creation_tok=_cache_write)
        except Exception:
            cost = None  # model not in price table → shown as n/a
        return {**base, "text": text, "in_tok": in_tok, "out_tok": out_tok, "latency": dt, "cost": cost,
                "cache_read_tok": _cache_read, "cache_write_tok": _cache_write,   # prompt-cache usage (feeds the receipt + reconcile)
                "effort": _effort_sent,   # the reasoning_effort ACTUALLY sent (post heal/drop) = chosen_effort — the metered
                #                           twin of the lane's applied-effort, so a pinned/metered_only row is verifiable
                # GUARDRAIL A per-call surface: the caller's requested tier, the effort truly chosen, and whether the pin
                # could NOT be honored — so spendrails/warden can READ and ABORT on it per call, not just via the counter.
                "requested_effort": _eff_requested, "chosen_effort": _effort_sent, "effort_unhonored": _eff_unhonored,
                "finish_reason": _finish, "executor": "api", "error": None}   # metered API path — say so, like a lane says its name
    except Exception as e:
        # error_type is the exception CLASS name — a structured signal (like an HTTP status or sqlite_errorname),
        # NOT the message prose. vendor_call uses it to tell a deadline (the vendor didn't answer in the budget:
        # APITimeoutError / ReadTimeout) from a transport fault (the connection broke / was refused), so the
        # coverage report can say WHY a vendor didn't answer instead of lumping both under transport_error.
        _status, _perr, _retry = _exc_detail(e)
        if _status in (429, 529):                       # a provider rate limit / overload → SELF-CALIBRATE the governor:
            try:                                        # read the vendor's OWN limit headers + Retry-After and TEACH it,
                from . import dispatch as _dispatch     # so the governor paces to the real ceiling and the first 429 is
                _tpm_seen, _rpm_seen = _ratelimit_from_resp(getattr(e, "response", None))   # the last. Best-effort: a
                try:                                    # learn failure must never turn a 429 result into a crash.
                    _ra_s = float(_retry) if _retry else None
                except (TypeError, ValueError):
                    _ra_s = None
                _dispatch.learn_rate_limit(prov, tpm=_tpm_seen, rpm=_rpm_seen, retry_after_s=_ra_s,
                                           source=f"{_status}-header")
            except Exception as _le:
                from . import gate as _lg
                if _lg.is_deliberate_stop(_le):         # a propagated spend refusal / deadline is NEVER swallowed
                    raise
                # else best-effort: a transient learn failure never turns the 429 result into a crash
        # TRANSPARENT TO THE CONSUMER: this is the metered API path (a lane miss/error is returned earlier with
        # executor=<lane>), so say `executor="api"` — a caller can now tell lane-vs-API from the result alone.
        # `cause` surfaces the real reason behind a generic wrapper (e.g. 'Connection error.' ← 'ConnectTimeout').
        return {**base, "latency": time.time() - t0, "error": str(e)[:140], "error_type": type(e).__name__,
                "status_code": _status, "provider_error": _perr, "retry_after": _retry,
                "executor": "api", "cause": _exc_cause(e)}


# ── a COMPLETE answer, or an explicit UNKNOWN — never a truncated body ───────────────────────────────────
# INPUT TWIN: this section guards the OUTPUT (a reply is never read as short when it was truncated). The mirror
# guard on the INPUT lives in llm_files.attach_whole — a prompt never silently carries a truncated FILE — and is
# reachable through call(files=[…]) above. The two are meant to be read together: same discipline, both ends.
# THE OUTPUT FLOOR, AND WHY IT IS NOT A PREDICTION.
#
# AXIS NOTE (read this before quoting 32k anywhere): TOKEN_FLOOR is an OUTPUT number — the reply budget. It is
# INDEPENDENT of the input. A large prompt never lowers it, and it never limits how much INPUT may be sent;
# input is bounded separately by the model's input window (pricing.max_input_tokens). There is no input default
# of any size — an unmeasured input is not capped at 32k or anything else. Do not describe this as an input cap.
#
# A cap has never controlled cost — you are billed for the tokens GENERATED, so an unused budget is free and
# a low budget saves exactly nothing. All a cap can do is destroy the answer. Every attempt to be clever
# about this number has cost money rather than saved it, because a truncated reply is a call you paid for
# and cannot use, and then you pay again for the retry.
#
# Predicting it from measured output is worse than it looks. On reasoning models (gpt-5/o-series) the hidden
# reasoning tokens are billed against max_tokens, and a measured p99 of VISIBLE output never saw them — so
# the prediction is systematically low on exactly the models that need the most room. A 4000-token budget
# spent entirely on reasoning returns a well-formed response whose text is "".
#
# So: start at the floor and let a measurement raise it, never lower it. The prediction can only add.
TOKEN_FLOOR = pricing.OUTPUT_FLOOR   # the OUTPUT floor lives in ONE home (pricing.OUTPUT_FLOOR=32K); adapters references it
MAX_TOKEN_CEILING = 128_000      # OUTPUT: absolute stop for the doubling retry — above the floor so retries have room
# The auto-heal LEARNS a model's output ceiling by halving until the provider accepts a budget. Below this floor a
# "success" is NOT evidence of a real output limit — no chat model caps output in the hundreds — it is a NON-budget
# 400 (a malformed request, a transient error) that merely happened to pass at a tiny budget. Recording that value
# POISONS the model's max_output for every future call (MEASURED: gpt-5-mini learned max_output=7, clamping the whole
# class to a ~7-token budget → 51% truncation). So the halving never learns a ceiling below this floor.
_MIN_LEARNED_MAX_OUTPUT = 1_024


def output_budget(model, requested=None, internal_tiny=False, vendor=None):
    """CANONICAL HOME for the OUTPUT max_tokens SENT on a call (docs/CANONICAL_CONCERNS.json: output_budget). No caller
    decides this — spendguard does. The budget IS the model's CEILING (pricing.output_ceiling: the real published max, or
    the 32K OUTPUT_FLOOR when unknown). Why: max_output is a CEILING billed on ACTUAL tokens generated — a high ceiling
    costs nothing and the ONLY failure mode is one set too LOW, which truncates a real answer. So a caller's `requested`
    max_tokens is IGNORED for a normal call; the ceiling already covers every output up to the model's real maximum, so
    there is no reasoning-empty, no structured-JSON-cut, no poison-clamp. Sizing the EXPECTED output for a cost estimate
    is a DIFFERENT concern (expected_output.expect) — never conflate the two: this is the ceiling, that is the guess.

    The ONE exception is an INTERNAL, deliberately-tiny call (`internal_tiny`: a connectivity / resolver / heal probe that
    wants a small bounded response) — it keeps its own `requested` value, still clamped to the ceiling so it can never
    exceed the model max. A provider that couples input+output against one context window and refuses the full ceiling is
    recovered by _call_guarded's downward heal — which halves the OUTPUT budget, never the INPUT (input is refused, not
    clipped, by _input_fits). `vendor` may be passed when the caller already knows it (the catalog ceiling tier)."""
    if vendor is None:
        try:
            vendor = provider_for(model)
        except Exception:
            vendor = None
    # Backstop = the 32K FLOOR (not MAX_TOKEN_CEILING): "if it is not published, the floor is 32000". A model with a
    # genuinely-published (or catalog) max returns THAT (even 128K); a model NOTHING knows returns the floor — the
    # conservative, no-truncation-below-floor default that is also safe for a no-heal lane (which can't recover a 400 on
    # an over-large budget). Publishing the real max (the price data) is how an unknown model earns a higher ceiling.
    ceiling = int(pricing.output_ceiling(vendor, model, TOKEN_FLOOR))
    if internal_tiny and requested is not None:
        try:
            return max(1, min(int(requested), ceiling))   # a deliberate tiny probe: respect it, never exceed the ceiling
        except (TypeError, ValueError):
            return ceiling
    return ceiling                                          # normal call: the ceiling; the caller's request is ignored


def _heal_token_budget(create_fn, start_budget, model):
    """Provider refused `start_budget` as too large an OUTPUT budget: halve until it accepts, then LEARN the accepted
    value as the model's max_output so the next call starts there. Returns the successful response, or None if none
    succeeded ABOVE `_MIN_LEARNED_MAX_OUTPUT`.

    The floor is the whole point: a 400 from a NON-budget cause (a malformed request, a transient error) also lands
    here and gets halved, and recording the tiny value where it happened to pass POISONS the model's max_output for
    every future call (MEASURED: gpt-5-mini learned max_output=7, clamping the class to a ~7-token budget). Below the
    floor the problem is not the budget — stop, and never record. Extracted from the inline ladder so this invariant
    is unit-testable (tests/test_token_budget_heal_floor.py)."""
    _try = int(start_budget)
    for _ in range(6):                                 # 32K -> 1K is five halvings; bounded either way
        _try //= 2
        if _try < _MIN_LEARNED_MAX_OUTPUT:
            return None                                # not a real output ceiling → don't shrink further, don't learn
        try:
            r = create_fn(_try)
        except Exception:
            continue                                   # still refused at this budget → keep halving
        try:
            from . import models as _mm, pricing as _pr
            # PLAUSIBILITY GUARD: LEARN a ceiling ONLY for a model whose real limit is UNKNOWN to the catalog. When
            # the published limits cache already knows it, THAT is authoritative — a heal landing below it is a
            # transient artifact (a non-budget 400 that happened to clear at budget/2), which is exactly the
            # 2000/7 poison. _call_guarded already clamps to the published ceiling BEFORE the send, so a
            # published-known model should never reach a budget-400 here; if it does, never overwrite the real
            # number with the halved guess. (The floor above stops the too-LOW extreme; this stops the poison.)
            if not _pr.max_output_tokens(model):
                _mm.add_fact(model, "max_output_tokens", _try,
                             source="auto-heal(refused a larger budget; no published ceiling)", verified=True)
        except Exception:
            pass                                       # learning is a bonus; the call already succeeded
        return r
    return None


def _input_fits(model, prompt, system, images=None):
    """(ok, detail) — does the INPUT fit the model's INPUT window?

    INPUT AND OUTPUT ARE INDEPENDENT AXES — the point people and agents keep getting wrong, so it is said here
    plainly. This function looks at the INPUT ONLY, and bounds it by the model's INPUT limit (max_input_tokens).
    It does not read, set, or shrink the output budget (max_tokens); and the output budget never limits how much
    input you may send. The two are bounded by DIFFERENT model numbers — input by `max_input_tokens`, output by
    `max_output_tokens` — and a large input does NOT reduce the reply size, nor a large reply the allowed input.
    The ONLY coupling is physical: an input OVER the input window means the call cannot be MADE at all (the
    provider rejects the whole request), which is the sole reason an over-window input is refused here — not any
    output consideration.

    Two INDEPENDENT input bounds, whichever is stricter:
      1. THE MODEL'S INPUT WINDOW (`pricing.max_input_tokens`, tokens) — the real per-model max. A request over it
         is REJECTED by the provider (not clipped), so this is a rail, not a tuned number; counted with the same
         tokenizer the gate uses. Wired here so an over-window prompt is caught BEFORE it bills, with the real
         number, instead of relying on a provider 400.
      2. A MEASURED PROVIDER PAYLOAD CEILING (`vendor_call.input_limit`, CHARS) — kept as a STRICTER override where
         a provider enforces a size limit tighter than the token window (measured on Moonshot by bisection).

    UNMEASURED IS NOT UNLIMITED, but a guard that refuses every UNKNOWN model is useless — so unknown on BOTH
    passes (never silently, the detail says so). A swallow inside this guard is worse than anywhere else (it
    disables the very check whose presence the reader trusts), so the failure path says so OUT LOUD."""
    try:
        from . import vendor_call, pricing
        vendor = provider_for(model)
        raw = model.split(":", 1)[1] if ":" in model else model
        text = (system or "") + "\n" + (prompt or "")
        # IMAGES add INPUT tokens the char short-circuit below cannot see (a photo is ~1.5k tokens with a tiny
        # text prompt), and they are counted by the PIXEL rule, not the text tokenizer — so add them explicitly.
        img_tok = _image_input_tokens(images, vendor, raw) if images else 0

        # 1) THE MODEL'S INPUT WINDOW (tokens) — independent of the output budget. Tokenize ONLY when the char
        # count could reach the window: a token is >= 1 char, so len(text) <= window ⇒ tokens <= window ⇒ it fits,
        # and small calls never pay for tokenization. Images force the tokenize (their tokens are not in `text`).
        win = pricing.max_input_tokens(raw)
        if win and (len(text) > int(win) or img_tok):
            try:
                from .gate import _content_tokens      # the exact tokenizer the gate already counts input with — one source
                tok = _content_tokens(text, provider=vendor, model=raw) + img_tok
            except Exception:
                tok = len(text) // 4 + img_tok         # rail fallback if the tokenizer is unavailable — the window still gets checked
            if tok > int(win):
                return False, (f"~{tok:,} input tokens exceed the model's {int(win):,}-token context window — "
                               f"split the input; a request over the window is rejected by the provider, not clipped")

        # 2) A MEASURED PROVIDER PAYLOAD CEILING (chars) — stricter override. record_input_limit's parameter is
        # `max_chars` (payload SIZE), so compare CHARS to it, never tokens (an earlier cut compared tokens to this
        # char limit and refused every payload ~4x too early — a guard that blocks correct work is a broken one).
        rec = vendor_call.input_limit(vendor, raw)
        lim = rec.get("max_chars") if isinstance(rec, dict) else rec
        if lim and len(text) > int(lim):
            return False, f"{len(text):,} chars exceed the measured payload ceiling of {int(lim):,} chars for this model"

        if not win and not lim:
            return True, "input window UNKNOWN for this model (unmeasured — not blocked, never silently unlimited)"
        return True, "input fits" + (f" · window {int(win):,} tok" if win else "") + (f" · ceiling {int(lim):,} ch" if lim else "")
    except Exception as e:
        # The guard failing is not the payload failing, so the call proceeds — the provider will reject an oversized
        # request itself (worse error, not a wrong answer). Said OUT LOUD: a silent "check unavailable" is
        # indistinguishable from "checked and fine", which is how this function once passed a payload unchecked.
        from . import config as _cfg
        _cfg.warn_once(f"[spendguard] the input-size check FAILED for {model} ({type(e).__name__}: "
                       f"{str(e)[:60]}) — the payload was NOT checked. This is a bug in the check.")
        return True, f"input check unavailable ({type(e).__name__})"


def _call_guarded(model, prompt, max_tokens=None, sig=None, retries=2, **kw):
    """The input and output token guards, run on EVERY call. Not a helper anyone has to remember.

    TWO INDEPENDENT guards on TWO independent axes, and this is the place to internalise that: `_input_fits`
    bounds the INPUT by the model's input window (max_input_tokens); the max_tokens block below sets the OUTPUT
    budget from the OUTPUT ceiling (max_output) plus measurement. Neither reads the other — a big input never
    shrinks the output budget, and the output budget never limits the input. The input guard may REFUSE the call
    (an over-window input cannot be sent at all); it never trims the prompt, and it never touches the reply size.

    An earlier version of this was a sibling function (`call_complete`) that callers opted into, and the
    result was measurable within the hour: 1 of 9 judging scripts used it, and a name-review re-run reported
    40 of 79 groups "UNREVIEWED" that were in fact just truncated. Safety a caller must remember to ask for
    is safety that will be missing exactly when the call matters. Both guards now live on the one path
    everything already takes.
    """
    _no_sub = kw.pop("_no_sub", False)
    _probe = kw.pop("_probe", False)                       # a reachability/connectivity probe: keep it tiny + fast,
    #                                                        never floored to reasoning headroom or grown on an empty
    from . import bulkgate
    # AUTO-APPLY the learned reasoning EFFORT for this (intent, model) — the effort twin of the reasoning-budget floor
    # below. ONLY when the caller set no explicit effort (an explicit reasoning always wins) and this is not a probe or
    # an internal resolver/heal call. A None fact leaves reasoning unset → the model's FAMILY FLOOR stands downstream
    # (models.apply_call_params), never a forced effort. The tier the effort bake-off measured cheapest-that-holds.
    if kw.get("reasoning") is None and not _probe and not getattr(_resolve_guard, "on", False) \
            and not getattr(_heal_guard, "on", False):
        try:
            from . import models as _mte, calls as _cte
            _eff_intent = (_cte.current() or {}).get("intent") or sig
            _learned_eff = _mte.effort_for(model, _eff_intent) if _eff_intent else None
            if _learned_eff:
                kw["reasoning"] = _learned_eff
        except Exception:
            pass                                           # a missing/broken fact store must never block the call
    # PROACTIVE LANE LOAD-BALANCING (Part 2): if this call's INTENT has a CONFIRMED substitute and the primary plan is
    # HOT while an acceptable substitute's plan is IDLE, run the substitute model instead — resolved through THIS same
    # guarded path (recursion), so the substitute gets its OWN output budget and input check, not the primary's. The
    # substitute is RECORDED as the model that answered (honesty), with substituted_from carrying the provenance.
    # Default OFF: no confirmed substitute for the intent → route_decision returns None → nothing changes.
    if not _no_sub and not getattr(_sub_guard, "on", False):
        try:
            from . import lane_balance, calls
            _sub, _why = lane_balance.route_decision((calls.current() or {}).get("intent"), model)
        except Exception:
            _sub, _why = None, ""
        if _sub and _sub != model:
            import sys as _sys
            _subkw = dict(kw)
            _adapt = None
            try:                                   # Stage 3: apply the RECORDED prompt adaptation for the target (mechanical)
                _adapt = lane_balance.adapted_system_for((calls.current() or {}).get("intent"), _sub)
            except Exception:
                _adapt = None
            if _adapt is not None:
                _subkw["system"] = _adapt
            print(f"[spendguard] lane-balance: {_why} — running {_sub} in place of {model} "
                  f"(recorded as {_sub}{'; prompt adapted' if _adapt is not None else ''})", file=_sys.stderr)
            _sub_guard.on = True                   # one hop: the substitute must not itself substitute (proactive OR reactive)
            try:
                r = _call_guarded(_sub, prompt, max_tokens=max_tokens, sig=sig, retries=retries, _no_sub=True, **_subkw)
            except Exception as _se:               # a substitute that RAISES (transport/etc.) must not sink the call —
                from . import gate as _gsub
                if _gsub.is_deliberate_stop(_se):  # …but a SpendGateRefused / deadline / containment HALTS: it is NEVER
                    raise                          # swallowed into a substitute error and continued on the original.
                r = {"error": f"substitute {_sub} raised: {type(_se).__name__}: {str(_se)[:130]}",
                     "error_type": type(_se).__name__}
            finally:
                _sub_guard.on = False
            if not r.get("error"):
                return {**r, "substituted_from": model, "substitution": _why, "prompt_adapted": _adapt is not None}
            # THE SUBSTITUTE FAILED (a non-deliberate error — a deliberate stop already re-raised above) — do NOT strand
            # the caller (7thsense 2026-09-25). The PROACTIVE bandit moved this work OFF the model the caller named onto
            # a substitute whose OWN metered fallback was unavailable on THIS box (no provider key / transport error),
            # and returning the substitute's error stranded a call the ORIGINAL model would have served — while a "lane
            # missed" outcome recovers, because THAT (reactive) path already falls back to the API. SHED the failed
            # (lane, model) arm briefly so this call's retries + the next few calls skip it (choose_arm and route_decision
            # both skip a cooling arm), then FALL THROUGH to run the ORIGINAL model here, guarded, with substitution OFF.
            try:
                _sfail_lane = _LANES.get(provider_for(_sub), (None,))[0]
                if _sfail_lane:
                    _lane_model_cool(_sfail_lane, _sub.split(":", 1)[1] if ":" in _sub else _sub)
            except Exception:
                pass
            print(f"[spendguard] lane-balance: substitute {_sub} FAILED ({str(r.get('error'))[:70]}) — shed it, "
                  f"running the ORIGINAL {model} so the call is not stranded", file=_sys.stderr)
            _no_sub = True                          # run the original ONCE here (no re-substitution), then fall through
    ok, detail = _input_fits(model, prompt, kw.get("system"), images=kw.get("images"))
    if not ok:
        return {"provider": provider_for(model), "model": model, "text": None, "in_tok": 0, "out_tok": 0,
                "latency": 0.0, "cost": None, "finish_reason": None, "truncated": None,
                "error": f"payload too large: {detail} — split it rather than letting the vendor clip it"}
    _explicit = max_tokens is not None
    _structured = kw.get("schema") is not None
    # An INTERNAL, deliberately-tiny call (a connectivity / resolver / effort-heal probe) is the ONE case that keeps its
    # own small max_tokens; every other caller's max_tokens is IGNORED (output_budget owns it — see below).
    _internal_tiny = bool(_probe or getattr(_resolve_guard, "on", False) or getattr(_heal_guard, "on", False))
    # sig NAMES the call-class for TELEMETRY + the ESTIMATE (keyed PER MODEL so Haiku never sizes Opus) + the guardrail-E
    # runaway norm — NOT for the budget, which is the model ceiling regardless. A caller may pass a raw INTENT (keyed
    # per-model here) OR an already-built 16-hex bulkgate.sig (used AS-IS, never double-keyed into a namespace no
    # registered estimate reads). No budget depends on sig any more, so a call without one is fine (it just isn't keyed).
    _is_sig = isinstance(sig, str) and len(sig) == 16 and all(c in "0123456789abcdef" for c in sig)
    _sig_key = (sig if _is_sig else bulkgate.sig(model, template_id=sig)) if sig else None
    _mx0 = (bulkgate.maxtokens(_sig_key, model=model) or {}) if _sig_key else {}   # guardrail-E pre-call runaway norm
    #        (before this call lands); pass `model` so a COLD class carries its reasoning-inclusive seed as the norm
    # THE OUTPUT BUDGET — the ONE home (output_budget; docs/CANONICAL_CONCERNS.json). The caller's max_tokens is IGNORED
    # for a normal call: spendguard owns the budget, which is the model's CEILING. max_output is billed on ACTUAL tokens,
    # so a high ceiling is free and the ONLY failure is one set too LOW (it truncates). The ceiling already covers a
    # structured JSON, a reasoning model's hidden tokens, and any prose length — so the old explicit / structured-floor /
    # reasoning-floor / clamp branches all collapse into this one call. An internal tiny probe keeps its deliberate value.
    if _explicit and not _internal_tiny:
        _warn_once_caller_maxtokens(sig or model, int(max_tokens))    # caller cap IGNORED — spendguard sets the ceiling
    max_tokens = output_budget(model, requested=max_tokens, internal_tiny=_internal_tiny)
    _cap = output_budget(model)                     # the doubling ladder's upper bound == the ceiling; a truncation AT it
    #                                                 means the output genuinely exceeds the model max (the jumbo case)
    attempt, budget = 0, int(max_tokens)
    while True:
        r = call(model, prompt, max_tokens=budget, _no_guard=True, no_substitution=_no_sub, **kw)
        if r.get("error"):
            return {**r, "truncated": None}                   # an errored call was not truncated, it failed
        trunc = bulkgate.is_truncated(r.get("finish_reason"), r.get("out_tok"), budget)
        # REASONING MODELS SPEND THE BUDGET WHERE YOU CANNOT SEE IT. On gpt-5/o-series the hidden reasoning
        # tokens are billed against max_tokens, so a cap that looks generous — 4000 — can be consumed
        # entirely by reasoning, and what comes back is a well-formed response whose visible text is "".
        # Usually the API says finish_reason="length" and the check above catches it. It does not always:
        # the model can stop cleanly having emitted only reasoning, and then nothing above fires, because
        # nothing was truncated in the ordinary sense — the answer was simply never written.
        #
        # An empty visible answer from a call that GENERATED TOKENS is not an answer. Left alone it is the
        # worst version of this whole defect: "" parses as nothing, reads as "no findings", and carries no
        # error at all. Treated as a truncation so it takes the same doubling retry, which is also the right
        # remedy — more budget is exactly what a reasoning model needs to get past thinking and start writing.
        if not trunc and (r.get("out_tok") or 0) > 0 and not (r.get("text") or "").strip():
            trunc = True
            r = {**r, "empty_answer": True}
        # A PROBE uses a DELIBERATELY-tiny cap; recording its truncation poisons the class recommend (a probe whose
        # visible out_tok is 1 makes recommend = 1*2 = 2) and fires a misleading "hit max_tokens=64" warning. It is
        # not a measurement of the class's real output need → do not record it at all.
        if _sig_key and not _probe:
            try:
                # An EMPTY reasoning reply (out_tok>0, no visible text) is NOT a real short output — record it as
                # TRUNCATED so bulkgate.maxtokens CENSORS it (it measured a too-small cap, not the work). Recorded as a
                # genuine tiny output it would drag the learned recommend DOWN and the class would re-fire low forever;
                # censoring it lets the successful heal's large out_tok become the recommend the next call starts from.
                _fr = "length" if r.get("empty_answer") else r.get("finish_reason")
                bulkgate.note_response(_sig_key, model, r.get("out_tok") or 0, max_tokens=budget,
                                       finish_reason=_fr)   # per-model key (see _sig_key above)
                # GUARDRAIL E — PER-CALL RUNAWAY BREAKER. out_tok many times the class's MEASURED p99 (norm read
                # PRE-call as _mx0, so the runaway can't inflate its own baseline) is a runaway the loose reasoning
                # ceiling never truncates — record it so it is VISIBLE (never aborted: a cut reasoning call still bills
                # for nothing; guardrail D's budget_usd cap bounds the $). No-op without a trustworthy norm.
                bulkgate.check_runaway(_sig_key, model, r.get("out_tok") or 0, norm=_mx0)
            except Exception:
                pass                                          # telemetry must not break the call
        if not trunc:
            return {**r, "truncated": False, "max_tokens_used": budget}
        attempt += 1
        _empty = bool(r.get("empty_answer"))
        # A REASONING-EMPTY is not "the answer was too long" — the model spent the whole cap THINKING and never began
        # to write. Returning text=None hands the caller nothing and the task re-fires forever; the only remedy is
        # MORE room. So an empty JUMPS to the reasoning floor and is NOT abandoned when `retries` (which bounds
        # long-answer doublings) is spent — only the model's own output ceiling stops it. Ordinary truncation keeps
        # the `retries` bound. A probe never grows (it is one deliberate tiny shot). Crucially, the heal that finally
        # succeeds records a NON-truncated out_tok via note_response above, so bulkgate.maxtokens LEARNS the real need
        # → the next call of this class STARTS high enough and never empties again (this is what ends the every-run re-fire).
        _can_grow = budget < int(_cap) and not _probe and (_empty or attempt <= retries)
        if not _can_grow:
            import sys as _sys
            _why = "empty (reasoning consumed the budget)" if _empty else "truncated"
            _sys.stderr.write(f"[spendguard] reply STILL {_why} at {budget} tokens after {attempt} attempt(s) — "
                              f"returning text=None so it cannot be read as a short answer.\n")
            _terr = f"{_why} at {budget} tokens"
            if _structured:
                _terr = (f"STRUCTURED (JSON) reply {_terr} — the object is INCOMPLETE, not empty; a caller must read "
                         f"this as 'truncated, retry larger', NEVER as 'no findings'")
            # finish_reason='length' + truncated=True make this a TYPED truncation (vendor_call classifies it TRUNCATED,
            # whose .text RAISES) so a cut structured body can never be read as a short/empty answer.
            return {**r, "text": None, "truncated": True, "max_tokens_used": budget,
                    "finish_reason": (r.get("finish_reason") or "length"), "error": _terr}
        budget = min(max(budget * 2, TOKEN_FLOOR if _empty else 0), int(_cap))


# `call` now does everything this did. Kept as an alias so the callers wired to it this morning keep working
# and so nothing reads as if there were two ways to make a call — there is one.
call_complete = call
