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
_lane_cooldown = {}   # lane name -> unix ts until which it is cooling
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


_cooldown_lock = threading.Lock()


def _cooldown_path():
    from . import config
    return config.HOME / "lane_cooldown.json"


_cooldown_io_warned = set()   # so a persistence failure is announced ONCE, not per call (and never silently)


def _warn_cooldown_io(kind, exc):
    """A cooldown-persistence failure is NOT harmless: it silently drops the guarantee that a quota-dead lane
    stays demoted across processes, so the next bulk run re-hammers it. Say so — once per failure kind — rather
    than swallow it. Dispatch still degrades to per-process cooling; the operator just learns the persistence is
    not working (e.g. SPENDGUARD_HOME unwritable, or a corrupt file to fix)."""
    if kind in _cooldown_io_warned:
        return
    _cooldown_io_warned.add(kind)
    import sys as _sy
    _sy.stderr.write(f"[spendguard] lane cooldowns NOT persisted ({kind}: {type(exc).__name__}: {str(exc)[:80]}) — "
                     f"a quota-cooled lane may be retried by a fresh bulk process. Fix {_cooldown_path()}.\n")


def _load_cooldowns():
    """Cross-PROCESS cooldowns. A bulk run is a FRESH process, so an in-memory cooldown never survives to the next
    `spendguard lanes --bulk` — a quota exhaustion ('resets in 162h') must outlive the process that hit it, or the
    next run hammers the dead lane again. Cooldowns persist to a small json under SPENDGUARD_HOME and are reloaded
    at import; expired entries are dropped on load. A read/parse failure is SURFACED (not swallowed) and degrades
    to per-process cooling — an unreadable file silently dropping every cooldown is exactly what lets the dead
    lane through."""
    global _lane_cooldown
    p = _cooldown_path()
    try:
        if not p.exists():
            return
        import json as _j
        now = time.time()
        _lane_cooldown = {k: float(v) for k, v in _j.loads(p.read_text()).items() if float(v) > now}
    except Exception as e:
        _warn_cooldown_io("load", e)      # a CORRUPT file must not read as 'no cooldowns' without a word


def _save_cooldowns():
    try:
        from . import config
        live = {k: v for k, v in _lane_cooldown.items() if v > time.time()}
        config.update_json(str(_cooldown_path()), lambda _d: live)     # atomic write (+~backup), like prices.json
    except Exception as e:
        _warn_cooldown_io("save", e)      # a write failure means this cool won't survive to the next bulk process


def _lane_cooling(lane):
    return time.time() < _lane_cooldown.get(lane, 0)


def _lane_cool(lane, seconds=None):
    """Cool a lane for `seconds` (default the pool cooldown). A quota exhaustion passes its parsed reset window so
    the lane stays down UNTIL it resets, instead of being retried every 900s only to re-fail. Persisted so a fresh
    bulk process honors it."""
    with _cooldown_lock:
        _lane_cooldown[lane] = time.time() + (float(seconds) if seconds else _pool_cooldown_s())
        _save_cooldowns()


_load_cooldowns()   # honor a still-active cooldown from a previous process (e.g. a quota reset window)


_lane_model_cooldown = {}   # (lane, model) -> unix ts: a specific model this lane REJECTED while the API served it.


def _lane_model_cooling(lane, model):
    return time.time() < _lane_model_cooldown.get((lane, model), 0)


def _lane_model_cool(lane, model):
    """Back off ONE (lane, model) after the lane failed for it WITHIN a proven-good size and the API then answered
    — a MODEL/content mismatch (e.g. codex 400s gpt-5-mini on a ChatGPT plan: "not supported with a ChatGPT
    account"), not the lane being down. Per-model so gpt-5.5 keeps riding codex the whole time; self-healing (it
    expires) so a model the plan later serves is retried. Without this the lane re-intercepts the rejected model on
    EVERY call — the '_learn_from_fallback' size axis alone can't see a model rejection."""
    _lane_model_cooldown[(lane, model)] = time.time() + _pool_cooldown_s()


# A lane failure is NOT always the lane being down. A CLI that ran the prompt as an AGENT and hit its turn limit,
# or a prompt that overran the plan model's context, is a PROMPT-vs-lane MISMATCH — the lane is fine for other
# prompts — so cooling the WHOLE lane for 900s (metering even small prompts after it) is the wrong response.
# Measured on a 4-LLM code review: one big file's claude-code result came back error_max_turns and cooled the
# lane. Rather than parse each lane's error TEXT to guess which kind it was (fragile, and every lane words it
# differently), the SYSTEM uses the fact it already has: it falls back to the API anyway, so the API OUTCOME on
# the SAME prompt settles it — the API answered where the lane did not ⇒ the lane was UNSUITABLE for this prompt
# (keep it, and route prompts this size straight to API); the API failed too ⇒ a real problem (cool the lane).
# One signal, every lane, and no interpretation of an error string.
_lane_big_prompt_ceiling = {}   # lane -> smallest prompt chars ABOVE a proven-good size that failed 'unsuitable'
_lane_ok_max = {}               # lane -> largest prompt chars the lane has SUCCESSFULLY answered


def _lane_note_ok(lane, prompt):
    """Record that the lane ANSWERED a prompt of this size. This is the proven-good watermark below which an
    'unsuitable' failure is read as content-specific, not a size limit — so a small anomaly can never lower the
    routing ceiling and disable a lane that demonstrably handles that size."""
    n = len(prompt or "")
    if n > _lane_ok_max.get(lane, 0):
        _lane_ok_max[lane] = n


def _lane_too_big(lane, prompt):
    """True when a prompt this size already provoked an 'unsuitable' failure on this lane (the lane failed but the
    API then answered) — route it straight to the API rather than pay the lane's cold start to fail again.
    In-process learning; resets each run."""
    ceil = _lane_big_prompt_ceiling.get(lane)
    return ceil is not None and len(prompt or "") >= ceil


def _learn_from_fallback(lane_name, prompt, api_failed, model=None):
    """The auto-route decision, taken from the API-fallback OUTCOME (never the lane's error text). api_failed is
    False when the API answered where the lane did not ⇒ the lane was UNSUITABLE for this prompt: keep the lane,
    and — ONLY when this failing size is LARGER than a size the lane has proven it can answer — learn a routing
    ceiling so bigger prompts skip it. A failure SMALLER than a proven-good size is content-specific, not a size
    limit, so it must not lower the ceiling and disable a working lane. api_failed is True ⇒ a real problem: cool
    the lane. Generalizes across every lane because the signal is a FACT (did the API answer?), not a string."""
    if api_failed:
        _lane_cool(lane_name)
        return "down"
    n = len(prompt or "")
    if n > _lane_ok_max.get(lane_name, 0):          # only a failure above the proven-good range is a size signal
        _lane_big_prompt_ceiling[lane_name] = min(_lane_big_prompt_ceiling.get(lane_name, n), n)
    elif model:                                     # WITHIN a size the lane can handle, yet it failed and the API
        _lane_model_cool(lane_name, model)          # answered → a MODEL/content mismatch (not a size limit): back
        #                                             off THIS model on THIS lane so it stops re-intercepting it.
    return "unsuitable"


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
def call(model, prompt, max_tokens=None, system=None, reasoning=None, schema=None, timeout_s=None,
         sig=None, retries=2, files=None, _no_guard=False, no_metered_fallback=False):
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
    empty output, and costs more — wrong for simple classify/extract calls)."""
    # INPUT-COMPLETENESS: fold whole, stamped, self-verified files into the prompt BEFORE the guards, so the
    # full payload is what _input_fits measures and a size overflow is refused here rather than clipped by the
    # vendor. Consumed here (not forwarded), so the _call_guarded → call(_no_guard=True) recursion below never
    # re-assembles. attach_many fails closed (PartialFileError) — a partial file never reaches a provider.
    if files:
        from . import llm_files
        _block, _ = llm_files.attach_many(files)
        prompt = _block + "\n" + prompt
    if not _no_guard:
        return _call_guarded(model, prompt, max_tokens=max_tokens, system=system, reasoning=reasoning,
                             schema=schema, timeout_s=timeout_s, sig=sig, retries=retries,
                             no_metered_fallback=no_metered_fallback)
    return _call_once(model, prompt, max_tokens=max_tokens, system=system, reasoning=reasoning,
                      schema=schema, timeout_s=timeout_s, no_metered_fallback=no_metered_fallback)


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


def _call_once(model, prompt, max_tokens=None, system=None, reasoning=None, schema=None, timeout_s=None,
               _skip_lane=False, no_metered_fallback=False):
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
    _lane = None if _skip_lane else _lane_for(prov)
    if _lane and (_lane_too_big(_lane[0], prompt) or _lane_model_cooling(_lane[0], raw)):
        _lane = None                                 # prompt too big for this lane, OR this MODEL was rejected here
        #                                              recently (the API served it) → straight to API, don't re-intercept
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
            s = lane_mod.run_prompt(prompt, system=_lane_sys, model=raw, timeout=_lane_timeout, reasoning=reasoning)
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
                calls.record(prov, raw, "subscription", 0.0,
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
            # shape it owns — see antigravity_exec._quota_backoff_s). This is the one failure the API-outcome
            # doctrine below cannot see: a quota-dead lane's metered API twin answers fine, so the fallback would
            # keep the lane "unsuitable-but-alive" and re-hand it work. Demote it UNTIL its reset window. The
            # routing layer acts on the seconds, never the error text — the text-blindness stays intact here.
            _lane_cool(lane_name, seconds=_ra)
        if no_metered_fallback:                        # caller opted out of ALL metered spend (--refuse-billed): a lane
            return {**base, "text": None, "cost": None, "executor": lane_name,   # MISS is an error row, NOT a paid
                    "error": f"refused: would bill metered API ({s.get('error')})"}   # retry — $0 by construction
        # LANE FAILED. REACTIVE FAILOVER (Part 2) FIRST: before paying the metered API, try a CONFIRMED substitute
        # PLAN for this intent — one hop, guarded against recursion. Routed through call() so the substitute resolves
        # its OWN budget and rides its OWN lane; if it answers, the primary lane is cooled (it failed) and the
        # substitution is recorded. Default OFF: no confirmed substitute → route_decision returns None → unchanged.
        if not getattr(_sub_guard, "on", False):
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
                    _lane_cool(lane_name)          # the primary lane failed → back off; the substitute carried the call
                    return {**_rr, "substituted_from": model, "substitution": _rwhy}
        # No substitute (or it also failed) → fall back to the API on the SAME prompt (this recurses with the lane
        # disabled, so the existing API path runs once, unchanged) and let its OUTCOME settle whether the lane was
        # unsuitable for this prompt (keep it) or genuinely down (cool it).
        out = _call_once(model, prompt, max_tokens=max_tokens, system=system, reasoning=reasoning,
                         schema=schema, timeout_s=timeout_s, _skip_lane=True)
        _kind = _learn_from_fallback(lane_name, prompt, bool(out.get("error")), model=raw)
        import sys as _sys
        if _kind == "unsuitable":
            # The ceiling is only LEARNED when the failure was ABOVE the lane's proven-good size; a
            # content-specific failure WITHIN that size returns "unsuitable" but leaves the ceiling unset. Read it
            # with .get — a bare index here KeyError'd and took down the very API fallback it was narrating.
            _ceil = _lane_big_prompt_ceiling.get(lane_name)
            if _ceil is not None:
                print(f"[spendguard] {lane_name} lane unsuitable for this prompt — kept for smaller ones; prompts "
                      f">= {_ceil} chars now route to API ({str(s['error'])[:60]})", file=_sys.stderr)
            else:
                print(f"[spendguard] {lane_name} lane unsuitable for this prompt (content-specific, within its "
                      f"proven-good size) — kept; the API answered it ({str(s['error'])[:60]})", file=_sys.stderr)
        else:
            print(f"[spendguard] {lane_name} lane unavailable — cooling {int(_pool_cooldown_s())}s; the API "
                  f"fallback also failed ({str(s['error'])[:60]})", file=_sys.stderr)
        return out
    key = config.api_key(spec["key_env"])
    if not key:
        return {**base, "error": f"no key ({spec['key_env']})"}
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
            kw = {"model": raw, "max_tokens": max_tokens, "messages": [{"role": "user", "content": prompt}]}
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
            msgs = ([{"role": "system", "content": system}] if system else []) + [{"role": "user", "content": prompt}]
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
                "finish_reason": _finish, "error": None}
    except Exception as e:
        # error_type is the exception CLASS name — a structured signal (like an HTTP status or sqlite_errorname),
        # NOT the message prose. vendor_call uses it to tell a deadline (the vendor didn't answer in the budget:
        # APITimeoutError / ReadTimeout) from a transport fault (the connection broke / was refused), so the
        # coverage report can say WHY a vendor didn't answer instead of lumping both under transport_error.
        _status, _perr, _retry = _exc_detail(e)
        return {**base, "latency": time.time() - t0, "error": str(e)[:140], "error_type": type(e).__name__,
                "status_code": _status, "provider_error": _perr, "retry_after": _retry}


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
            from . import models as _mm
            _mm.add_fact(model, "max_output_tokens", _try,
                         source="auto-heal(provider refused a larger budget)", verified=True)
        except Exception:
            pass                                       # learning is a bonus; the call already succeeded
        return r
    return None


def _input_fits(model, prompt, system):
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

        # 1) THE MODEL'S INPUT WINDOW (tokens) — independent of the output budget. Tokenize ONLY when the char
        # count could reach the window: a token is >= 1 char, so len(text) <= window ⇒ tokens <= window ⇒ it fits,
        # and small calls never pay for tokenization.
        win = pricing.max_input_tokens(raw)
        if win and len(text) > int(win):
            try:
                from .gate import _content_tokens      # the exact tokenizer the gate already counts input with — one source
                tok = _content_tokens(text, provider=vendor, model=raw)
            except Exception:
                tok = len(text) // 4                   # rail fallback if the tokenizer is unavailable — the window still gets checked
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
    ok, detail = _input_fits(model, prompt, kw.get("system"))
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
    # CLAMP TO WHAT THE MODEL ACTUALLY ACCEPTS. A floor above a model's own maximum is a 400, so the floor
    # is bounded by the documented/learned limit — min(model_max, max(floor, predicted)). When the limit is
    # UNKNOWN the floor still goes out: the adapter's downward retry halves until the provider accepts and
    # records what it accepted, so an unknown model self-corrects instead of being capped by a guess.
    _cap = pricing.max_output(model.split(":", 1)[-1])   # STRIP the provider prefix: pricing.normalize() strips date/
    #                                                      -latest/-codex but NOT "provider:model", so a qualified id
    #                                                      like "openai:gpt-5.5" missed its output cap and sent the
    #                                                      floor/predicted over the model's real ceiling.
    if _cap:
        max_tokens = min(int(max_tokens), int(_cap))
    attempt, budget = 0, int(max_tokens)
    while True:
        r = call(model, prompt, max_tokens=budget, _no_guard=True, **kw)
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
