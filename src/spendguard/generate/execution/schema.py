"""Derive schema capabilities and generation support, enforce strict-schema expressibility (including the SchemaNotStrictExpressible failure), and parse structured replies."""
import json

from .execution_accessors import SCHEMA_STRICT


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
    if schema_capability(kind) == SCHEMA_STRICT and kind == "anthropic":
        return {"tools": [{"name": name, "description": "Return the result in this exact shape.",
                           "input_schema": _provider_schema(schema)},
                          ],
                "tool_choice": {"type": "tool", "name": name}}
    if schema_capability(kind) == SCHEMA_STRICT and kind == "openai":
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


SCHEMA_JSON_OBJECT = "json_object"    # JSON is guaranteed, the SHAPE is not — output_contract validates it


def schema_capability(provider):
    """How strongly `provider`'s METERED path can hold output to a JSON schema: SCHEMA_STRICT (anthropic/openai — the
    vendor enforces the exact shape) or SCHEMA_JSON_OBJECT (every other OpenAI-compatible vendor — JSON guaranteed, the
    shape is not). Derived from _schema_kind so it cannot disagree with the request json_schema_request actually builds.
    A LANE is prompt-only regardless (a CLI completion carries no response_format/tool param), so a lane NEVER has
    strict capability even for a strict-capable vendor — which is exactly why a strict schema on a prompt-only lane
    must route to that vendor's metered path (the capability-aware auto-route)."""
    return SCHEMA_STRICT if _schema_kind(provider) in ("anthropic", "openai") else SCHEMA_JSON_OBJECT


SDK_CONTRACT_UNAVAILABLE_REASON = "provider SDK generation contract unavailable; requested knobs were not sent"


def generation_support_details(provider, model):
    """The subset of GEN_KNOBS (temperature, top_p, seed) that `provider`/`model`'s METERED API actually honors — the
    SSOT that maps the caller's ONE standardized determinism surface onto each vendor's real capability, so the same
    `adapters.call(..., measurement=True, temperature=0, seed=…)` is correct across providers and a knob the vendor
    cannot take is RECORDED as dropped, never a silent 400 and never silently ignored. A FACT about the vendor API
    (like schema_capability), not a judgement:
      • `seed` is an OpenAI (the real 'openai' provider) parameter; anthropic and the other OpenAI-compatible vendors
        (gemini/zai/moonshot/deepseek) have no seed field, so seed is unsupported there.
      • `temperature`/`top_p` are sampling knobs a REASONING model rejects or constrains (OpenAI 400s 'temperature
        does not support 0 with this model'; anthropic ties temperature to the thinking budget), so they are dropped
        for a reasoning model (reasons_by_default) and determinism there rests on seed where the vendor has it.
    Returns ``(supported, unavailable_reason)``; the caller records both any dropped knobs and why the installed
    SDK contract could not be inspected.  The reason is None when the contract was inspectable."""
    from ... import models as _gm
    support = set()
    if not _gm.reasons_by_default(model):                 # non-reasoning: sampling knobs are honored
        support |= {"temperature", "top_p"}
    if provider == "openai":                              # seed is OpenAI-only (compat/anthropic have no seed param)
        support.add("seed")
    unavailable_reason = None
    if provider == "anthropic":
        # The SDK's stream surface is narrower than messages.create in some releases.  In particular, 1.15.0
        # rejects sampling knobs in Python before opening a request.  The gate wrapper preserves __wrapped__, so
        # inspect.signature follows through to the installed SDK's real contract.  Intersecting with that contract
        # keeps this version-independent and records unsupported requested knobs through gen_params_dropped.
        try:
            import inspect
            import anthropic
            stream_parameters = inspect.signature(anthropic.resources.messages.Messages.stream).parameters
            support &= set(stream_parameters)
        except (ImportError, AttributeError, TypeError, ValueError):
            support.clear()                               # cannot prove the SDK accepts a knob → never send it
            unavailable_reason = SDK_CONTRACT_UNAVAILABLE_REASON
    # SUBTRACT knobs the vendor has TOLD us it does not honor — a measured 400 like '`temperature` is deprecated for
    # this model' recorded as a model fact (models.mark_gen_unsupported), so the SSOT reflects REALITY instead of an
    # assumption and NEVER advertises a knob that 400s. Claude 5 (e.g. claude-haiku-5-5) deprecated BOTH temperature
    # and top_p (measured 2026-10-09); the 400 path self-records any future model the same way.
    try:
        support -= _gm.unsupported_gen_knobs(model)
    except Exception:
        pass
    return support, unavailable_reason


def generation_support(provider, model):
    """Backward-compatible supported-knob view; observability is consumed by the metered call boundary."""
    return generation_support_details(provider, model)[0]


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
            from ... import output_contract as _oc
            obj, _ = _oc._as_obj(r["text"])
        except Exception:
            obj = None
    return obj if isinstance(obj, dict) else None
