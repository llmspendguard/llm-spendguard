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
                  "prefixes": ("gpt-", "o1", "o3", "chatgpt"), "kind": "openai"},
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
_LANES = {"anthropic": ("claude-code", "subscription_exec"), "openai": ("codex", "codex_exec"),
          "zai": ("zai-coding", "zai_exec"),                    # z.ai GLM Coding Plan — Anthropic-compatible flat-fee endpoint
          "gemini": ("gemini", "antigravity_exec")}             # Google Antigravity CLI (`agy`) — Gemini plan lane
from . import resource_state   # AXIS-1 of the resource_state migration: cooldowns now live in the unified store
_sub_guard = threading.local()   # one-hop lane-substitution guard: while a substitute call is in flight (proactive OR
                                 # reactive), no nested substitution — a substitute failing does not chain to a third.
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
        return {"response_format": {"type": "json_schema",
                                    "json_schema": {"name": name, "schema": _openai_strict(schema),
                                                    "strict": True}}}
    # OpenAI-COMPATIBLE, not OpenAI. json_object guarantees parseable JSON and nothing about the shape, so
    # the shape has to travel in the PROMPT — the caller gets it appended rather than being left to hope.
    return {"response_format": {"type": "json_object"},
            "_schema_prompt": "Return ONLY a JSON object conforming exactly to this JSON Schema. No prose, "
                              "no code fence, no explanation.\n" + json.dumps(_provider_schema(schema))}


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
def _maybe_credit_advisor(requested, r):
    """When a call was SUBSTITUTED onto a CHEAPER, METERED model than the one requested, book the difference as a
    guarded 'advisor' saving (counterfactual — the requested model is the honest baseline). SKIPPED for a $0
    plan-lane substitution: that avoided-API value is the EST-VALUE axis, so booking it here too would double-count.
    The dominant routing saving (plan-served) therefore stays on est-value; this credits ONLY the genuinely-
    uncaptured metered→cheaper swap. Best-effort; never raises into the call path."""
    try:
        if not isinstance(r, dict) or not r.get("substituted_from"):
            return
        cost = r.get("cost")
        if not cost or float(cost) <= 0:          # $0 → plan-served = est-value axis, not a savings-ledger row
            return
        from . import pricing, guard
        base = pricing.realtime_cost(requested, int(r.get("in_tok") or 0), int(r.get("out_tok") or 0))
        if base and float(base) > float(cost):
            guard.record_saving("advisor", float(base) - float(cost))
    except Exception:
        pass


def call(model, prompt, max_tokens=None, system=None, reasoning=None, schema=None, timeout_s=None,
         sig=None, retries=2, files=None, _no_guard=False, no_metered_fallback=False, images=None,
         no_substitution=False):
    """Run one prompt against one model. Returns a result dict (never raises).

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
    and so each reply feeds that measurement. `reasoning` (minimal|low|medium|high) sets reasoning effort for
    gpt-5/o-series models; defaults to 'minimal' for them (default-medium reasoning eats the token budget →
    empty output, and costs more — wrong for simple classify/extract calls).

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
    if not _no_guard:
        r = _call_guarded(model, prompt, max_tokens=max_tokens, system=system, reasoning=reasoning,
                          schema=schema, timeout_s=timeout_s, sig=sig, retries=retries,
                          no_metered_fallback=no_metered_fallback, images=images, _no_sub=no_substitution)
    else:
        r = _call_once(model, prompt, max_tokens=max_tokens, system=system, reasoning=reasoning,
                       schema=schema, timeout_s=timeout_s, no_metered_fallback=no_metered_fallback, images=images,
                       _no_sub=no_substitution)
    _maybe_credit_advisor(model, r)   # metered substitution to a CHEAPER model → guarded 'advisor' saving (savings tally)
    return r


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
    a parameter, and ignores the reasoning kwarg). No reasoning, or a value with no agy spelling (e.g. 'minimal'),
    returns the id unchanged — the lane then runs its default tier, never an invented one. Any existing tier on
    the id is REPLACED, so an explicit reasoning argument wins over a stale suffix."""
    if not reasoning:
        return model_id
    tier = str(reasoning).strip().lower()
    if tier not in _GEMINI_REASONING_TIERS:
        return model_id
    _base, _existing = _split_gemini_reasoning(model_id)
    return _base + "-" + tier


def metered_fallback_id(provider, use_name):
    """The metered-API model id equivalent to a lane's USE-NAME — the mapping that lets a lane call fall BACK to the
    provider's paid API when the lane is down/exhausted, WITHOUT the lane's own naming breaking the call. This is the
    ONE place that equivalence lives, so the live fallback (_call_once) and the lane-fallback audit can never drift.
    For gemini/agy the reasoning level rides the id as a SUFFIX (gemini-3.7-flash-medium) while the metered API wants
    the BARE id + a reasoning PARAMETER, so the suffix is split off; every other provider's lane id already IS its
    metered id. Returns (metered_id, reasoning_level_or_None)."""
    if provider == "gemini":
        return _split_gemini_reasoning(use_name)
    return use_name, None


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
        # Floor the lane deadline (LANE_MIN_TIMEOUT_S) so a slow CLI/plan call is not timed out by a metered
        # call's tight budget and churned to the paid API; cap it at the lane's own TIMEOUT_S so a hung lane is
        # still bounded. The lane is $0, so a generous wait costs latency, not money.
        _lane_cap = int(getattr(lane_mod, "TIMEOUT_S", 300))
        _lane_timeout = min(_lane_cap, max(int(timeout_s or 0), int(LANE_MIN_TIMEOUT_S)))
        try:
            # AGY namespace: effort rides the MODEL-ID SUFFIX, and agy ignores the reasoning kwarg. So a bare id
            # + reasoning=medium must be respelled gemini-…-flash-medium here, or the tier is silently dropped and
            # the lane runs its default. Non-gemini lanes take the id unchanged.
            _lane_model = _compose_gemini_reasoning(raw, reasoning) if prov == "gemini" else raw
            s = lane_mod.run_prompt(prompt, system=_lane_sys, model=_lane_model, timeout=_lane_timeout, reasoning=reasoning)
        except Exception as _le:                       # a lane MUST return an {error} dict, never raise — but a lane
            s = {"error": f"{lane_name} lane raised: {str(_le)[:120]}"}   # bug that throws must degrade, not crash call()
        if not isinstance(s, dict):                    # a non-dict is also a broken contract → treat it as an error
            s = {"error": f"{lane_name} lane returned {type(s).__name__}, not a result dict"}
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
            try:
                from . import calls
                calls.record_call(prov, raw, "subscription", 0.0,
                             in_tok=s.get("in_tok", 0), out_tok=s.get("out_tok", 0), latency=s.get("latency"),
                             executor=lane_name)     # WHICH plan served it — a stored fact, not a provider-guess
            except Exception:
                pass
            return {**base, "text": s["text"], "in_tok": s.get("in_tok", 0), "out_tok": s.get("out_tok", 0),
                    "latency": s.get("latency", 0.0), "cost": 0.0, "executor": lane_name, "error": None}
        if not s.get("error"):                         # no error but not a usable answer → say WHY (empty vs off-shape)
            s = {**s, "error": (f"{lane_name} lane returned no usable text (empty/whitespace)" if not _txt
                                else f"{lane_name} lane output did not satisfy the requested shape → API")}
        _ra = s.get("retry_after_s") if isinstance(s, dict) else None
        if _ra:
            # A STRUCTURED quota/exhaustion signal the lane EXECUTOR parsed from its own CLI's envelope (a known
            # shape it owns — see antigravity_exec._reset_window_s). This is the one failure the API-outcome
            # doctrine below cannot see: a quota-limited lane's metered API twin answers fine, so the fallback would
            # otherwise read the miss as a SIZE limit and pin a ceiling. Demote it until its reset window — but
            # CAPPED (_max_quota_cool_s), because an oscillating quota (agy) must be re-tested, not bypassed for
            # days. `transient` then tells _learn_from_fallback this was quota, so it learns NO size ceiling.
            _lane_cool(lane_name, seconds=min(float(_ra), _max_quota_cool_s()), reason="quota")
        if no_metered_fallback:                        # caller opted out of ALL metered spend (--refuse-billed): a lane
            return {**base, "text": None, "cost": None, "executor": lane_name,   # MISS is an error row, NOT a paid
                    "error": f"refused: would bill metered API ({s.get('error')})"}   # retry — $0 by construction
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
        out = _call_once(model, prompt, max_tokens=max_tokens, system=system, reasoning=reasoning,
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
                kw["system"] = system
            if schema is not None:
                kw.update(json_schema_request("anthropic", schema))
            # STREAM, always. The SDK REFUSES a non-streaming request whose max_tokens implies a run over ten
            # minutes ("Streaming is required for operations that may take longer than 10 minutes"), and the
            # threshold is the SDK's, not ours — guessing it would be a magic number that silently rots when
            # they change it. Streaming has no such bound, returns the identical final message and usage, and
            # measured 20/20 with no latency penalty. So the cap can be sized from measured need, which is the
            # whole point of a termination bound, without the transport vetoing it.
            with c.messages.stream(**kw) as s:
                m = s.get_final_message()
            # With a forced tool the answer arrives as tool_use.input, not as text — reading only text blocks
            # would return "" and look exactly like the empty-response failure.
            tu = [b for b in m.content if getattr(b, "type", None) == "tool_use"]
            text = (json.dumps(tu[0].input) if tu
                    else "".join(b.text for b in m.content if getattr(b, "type", None) == "text"))
            in_tok, out_tok = m.usage.input_tokens, m.usage.output_tokens
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
                sk = json_schema_request("openai" if prov == "openai" else "compat", schema)
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
            try:
                from . import models as _mf
                _mf.apply_call_params(raw, okw, dialect="openai")
            except Exception:
                pass                                        # a missing fact store must not break the call
            try:                                              # gpt-5+ require max_completion_tokens; older models take max_tokens
                r = c.chat.completions.create(max_completion_tokens=max_tokens, **okw)
            except Exception as e:
                # WHICH PARAMETER, FROM THE TYPED FIELD — not from the message text. These branches matched
                # `"response_format" in str(e)` and `"reasoning_effort" in str(e)`, so an error that merely
                # MENTIONED a parameter (a validation error listing the whole request, a message quoting the
                # body back) took the retry path and silently dropped a parameter the caller asked for —
                # and the schema being dropped is precisely how "required" fields come back as zeros.
                from . import models as _mp
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
                            lambda b: c.chat.completions.create(max_completion_tokens=b, **okw), max_tokens, raw)
                        if r is not None:
                            break                          # accepted at a plausible budget → learned + done
                        continue                           # nothing above the floor worked → try the next rung
                    if _rung == "_token_dialect":
                        # Older endpoints take max_tokens, gpt-5+ take max_completion_tokens. A dialect
                        # difference, not a capability one — nothing is dropped, the other spelling is used.
                        try:
                            r = c.chat.completions.create(max_tokens=max_tokens, **okw)
                            break
                        except Exception as e2:
                            _last = e2
                            continue
                    if _rung not in okw:
                        continue                          # not sent, so it cannot be what was refused
                    _saved = okw.pop(_rung)
                    try:
                        r = c.chat.completions.create(max_completion_tokens=max_tokens, **okw)
                        # SAY SO. A silently-dropped parameter makes the call succeed and makes any probe
                        # of "does this endpoint support X" answer yes — the fallback that keeps the system
                        # robust is exactly what blinds discovery. Recorded on the result so a caller can
                        # tell "it worked" from "it worked WITHOUT what you asked for".
                        base.setdefault("dropped", []).append(_rung)
                        if _rung == "response_format":
                            import sys as _s
                            print(f"[spendguard] {prov}/{raw} rejected response_format ({str(e)[:70]}) — "
                                  f"sending unenforced; output_contract still validates.", file=_s.stderr)
                        break
                    except Exception as e2:
                        okw[_rung] = _saved               # that rung was not the problem — put it back
                        _last = e2
                if r is None:
                    raise _last
            text = r.choices[0].message.content
            in_tok, out_tok = r.usage.prompt_tokens, r.usage.completion_tokens
            _finish = getattr(r.choices[0], "finish_reason", None)     # "length" when it hit the cap
        dt = time.time() - t0
        try:
            cost = pricing.realtime_cost(raw, in_tok, out_tok)
        except Exception:
            cost = None  # model not in price table → shown as n/a
        return {**base, "text": text, "in_tok": in_tok, "out_tok": out_tok, "latency": dt, "cost": cost,
                "finish_reason": _finish, "executor": "api", "error": None}   # metered API path — say so, like a lane says its name
    except Exception as e:
        # error_type is the exception CLASS name — a structured signal (like an HTTP status or sqlite_errorname),
        # NOT the message prose. vendor_call uses it to tell a deadline (the vendor didn't answer in the budget:
        # APITimeoutError / ReadTimeout) from a transport fault (the connection broke / was refused), so the
        # coverage report can say WHY a vendor didn't answer instead of lumping both under transport_error.
        _status, _perr, _retry = _exc_detail(e)
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
TOKEN_FLOOR = 32_000             # OUTPUT reply budget — never send less unless the CALLER named a number; NOT an input cap
MAX_TOKEN_CEILING = 128_000      # OUTPUT: absolute stop for the doubling retry — above the floor so retries have room
# The auto-heal LEARNS a model's output ceiling by halving until the provider accepts a budget. Below this floor a
# "success" is NOT evidence of a real output limit — no chat model caps output in the hundreds — it is a NON-budget
# 400 (a malformed request, a transient error) that merely happened to pass at a tiny budget. Recording that value
# POISONS the model's max_output for every future call (MEASURED: gpt-5-mini learned max_output=7, clamping the whole
# class to a ~7-token budget → 51% truncation). So the halving never learns a ceiling below this floor.
_MIN_LEARNED_MAX_OUTPUT = 1_024


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
    from . import bulkgate
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
            finally:
                _sub_guard.on = False
            return {**r, "substituted_from": model, "substitution": _why, "prompt_adapted": _adapt is not None}
    ok, detail = _input_fits(model, prompt, kw.get("system"), images=kw.get("images"))
    if not ok:
        return {"provider": provider_for(model), "model": model, "text": None, "in_tok": 0, "out_tok": 0,
                "latency": 0.0, "cost": None, "finish_reason": None, "truncated": None,
                "error": f"payload too large: {detail} — split it rather than letting the vendor clip it"}
    _explicit = max_tokens is not None
    if not _explicit and not sig:
        raise ValueError(
            "call needs either an explicit max_tokens or a `sig` naming the call-class, so the budget is "
            "either something you chose deliberately or something measured — never a literal nobody picked.")
    _predicted = int((bulkgate.maxtokens(sig) or {}).get("recommend") or 0) if sig else 0
    if _explicit:
        # The caller named a number, so they meant it — a 16-token connectivity probe is a legitimate,
        # deliberate choice, and token_caps has a recorded verdict for every such literal in this tree.
        # A measurement may still RAISE it; nothing may lower it.
        max_tokens = max(int(max_tokens), _predicted)
    else:
        # NOBODY CHOSE A NUMBER, SO START HIGH. Floor first, prediction only on top. This previously read
        # `max(caller, recommend) or CEILING`, which used the ceiling only when BOTH were zero — so a
        # measured recommend of 400 produced a 400-token budget, and the floor never applied to the calls
        # that most needed it. Costing nothing to over-provision and everything to under-provision, the
        # asymmetry only points one way.
        max_tokens = max(TOKEN_FLOOR, _predicted)
    # CLAMP TO THE MODEL'S PUBLISHED OUTPUT CEILING — output = min(max(provided|predicted, floor), model_max).
    # model_max must come from an AUTHORITATIVE catalog, NOT the learned per-model max_output FACT: that fact is
    # auto-heal's guess and has POISONED the clamp BOTH ways — gpt-5-nano learned max_output=2000 and under-truncated
    # every answer, while a model with NO fact (gpt-5.4-nano) had NO clamp at all, so a poisoned bulkgate
    # recommend (146,576, above the max EVER observed) went out over a 128,000-token model and 400'd every call.
    # Authority order via the SINGLE shared resolver (pricing.output_ceiling): published limits cache → live-/models
    # catalog → the (poison-prone) learned fact → the absolute MAX_TOKEN_CEILING backstop when NOTHING knows the
    # model. The backstop gives the Anthropic path (which has no downward heal) the same protection as OpenAI-compat:
    # a poisoned recommend can never send an absurd budget on ANY provider. For an unknown-ceiling model on
    # OpenAI-compat, the downward heal below still recovers a genuinely-lower real ceiling (and learns it — guarded
    # to not overwrite a published one). The resolver strips the "provider:" prefix so the cap is never missed.
    try:
        _vendor = provider_for(model)           # only the (best-effort) catalog tier needs it; a provider-less,
    except Exception:                           # unregistered model must still get clamped to the backstop, not
        _vendor = None                          # raise here — the old chain caught this inside its try, so preserve it
    _cap = pricing.output_ceiling(_vendor, model, MAX_TOKEN_CEILING)
    max_tokens = min(int(max_tokens), int(_cap))
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
        if sig:
            try:
                bulkgate.note_response(sig, model, r.get("out_tok") or 0, max_tokens=budget,
                                       finish_reason=r.get("finish_reason"))
            except Exception:
                pass                                          # telemetry must not break the call
        if not trunc:
            return {**r, "truncated": False, "max_tokens_used": budget}
        attempt += 1
        if attempt > retries or budget >= MAX_TOKEN_CEILING:
            import sys as _sys
            _sys.stderr.write(f"[spendguard] reply STILL truncated at {budget} tokens after {attempt} "
                              f"attempt(s) — returning text=None so it cannot be read as a short answer.\n")
            return {**r, "text": None, "truncated": True, "max_tokens_used": budget,
                    "error": f"truncated at {budget} tokens"}
        budget = min(budget * 2, MAX_TOKEN_CEILING)


# `call` now does everything this did. Kept as an alias so the callers wired to it this morning keep working
# and so nothing reads as if there were two ways to make a call — there is one.
call_complete = call
