"""Provider adapters for the `compare` harness.

Most providers expose an OpenAI-compatible API, so adding one is a single registry entry
(name, base_url, key env, model prefixes). Anthropic uses its own SDK. Add more at runtime
with register_provider(...). Calls go through the openai/anthropic SDKs, so the spend gate
already meters + budgets them.
"""
import time
import json
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
    """Resolve provider from a model id. Accepts explicit 'provider:model' too."""
    if ":" in model:
        return model.split(":", 1)[0]
    for name, p in PROVIDERS.items():
        if model.startswith(p["prefixes"]):
            return name
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
_LANES = {"anthropic": ("claude-code", "subscription_exec"), "openai": ("codex", "codex_exec")}
_lane_cooldown = {}   # lane name -> unix ts until which it is cooling


def _pool_cooldown_s():
    import os as _os
    try:
        return float(_os.environ.get("SPENDGUARD_POOL_COOLDOWN_S")
                     or config._cfg_get("advisor", "pool_cooldown_s", 900))
    except Exception:
        return 900.0


def _lane_cooling(lane):
    return time.time() < _lane_cooldown.get(lane, 0)


def _lane_cool(lane):
    _lane_cooldown[lane] = time.time() + _pool_cooldown_s()


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


# DEFAULT: no cap. `max_tokens=512` sat here and quietly capped every caller who did not name a number —
# including callers that deliberately pass none precisely so the measured budget applies. _call_guarded
# resolves None from the call-class's OWN measured history (autotune `recommend`, else 2048), which is the
# mechanism this package built for exactly this and which a default of 512 skipped entirely.
#
# A cap was never cost control: you are billed for the tokens GENERATED, so a low cap saves nothing and
# instead truncates the answer — and a truncated JSON body reads downstream as "no findings" rather than
# "no answer". That is the whole recurring failure. The number now comes from measurement or from the
# caller, never from a literal nobody chose.
def call(model, prompt, max_tokens=None, system=None, reasoning=None, schema=None, timeout_s=None,
         sig=None, retries=2, _no_guard=False):
    """Run one prompt against one model. Returns a result dict (never raises).

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

      INPUT   the prompt is measured against the recorded input limit for (vendor, model) BEFORE sending.
              Over it → an explicit error, not a request the vendor silently clips.
      OUTPUT  a reply the provider marks as cut off is RETRIED at double the budget; if it still will not
              fit, text=None and truncated=True, so a truncated body can never be read as a short answer.

    `sig` names the call-class so the budget comes from its measured p99 instead of the 512 default, and so
    each reply feeds that measurement. `reasoning` (minimal|low|medium|high) sets reasoning effort for
    gpt-5/o-series models; defaults to 'minimal' for them (default-medium reasoning eats the token budget →
    empty output, and costs more — wrong for simple classify/extract calls)."""
    if not _no_guard:
        return _call_guarded(model, prompt, max_tokens=max_tokens, system=system, reasoning=reasoning,
                             schema=schema, timeout_s=timeout_s, sig=sig, retries=retries)
    return _call_once(model, prompt, max_tokens=max_tokens, system=system, reasoning=reasoning,
                      schema=schema, timeout_s=timeout_s)


def _call_once(model, prompt, max_tokens=None, system=None, reasoning=None, schema=None, timeout_s=None):
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
    _lane = _lane_for(prov)
    if _lane:
        lane_name, lane_mod = _lane
        s = lane_mod.run_prompt(prompt, system=system, model=raw)   # honor the chosen tier on the plan too
        if not s.get("error"):
            try:
                from . import calls
                calls.record(prov, raw, "subscription", 0.0,
                             in_tok=s["in_tok"], out_tok=s["out_tok"], latency=s["latency"])
            except Exception:
                pass
            return {**base, "text": s["text"], "in_tok": s["in_tok"], "out_tok": s["out_tok"],
                    "latency": s["latency"], "cost": 0.0, "executor": lane_name, "error": None}
        _lane_cool(lane_name)
        import sys as _sys
        print(f"[spendguard] {lane_name} lane unavailable ({s['error']}) — cooling {int(_pool_cooldown_s())}s, "
              f"falling back to metered API", file=_sys.stderr)
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
            c = anthropic.Anthropic(api_key=key, timeout=timeout_s) if timeout_s else anthropic.Anthropic(api_key=key)
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
            c = (OpenAI(api_key=key, base_url=spec["base_url"], timeout=timeout_s) if timeout_s
                 else OpenAI(api_key=key, base_url=spec["base_url"]))
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
                okw["reasoning_effort"] = reasoning        # explicit caller argument: applied, then respected
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
                _RUNGS = ("response_format", "reasoning_effort", "_token_dialect")
                if _bad in ("response_format", "json_schema"):
                    _ladder = ("response_format",)
                elif _bad == "reasoning_effort":
                    _ladder = ("reasoning_effort",)
                elif _bad in ("max_tokens", "max_completion_tokens"):
                    _ladder = ("_token_dialect",)
                else:
                    _ladder = _RUNGS                     # unattributable: try each applicable rung
                r = None
                _last = e
                for _rung in _ladder:
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
        return {**base, "latency": time.time() - t0, "error": str(e)[:140]}


# ── a COMPLETE answer, or an explicit UNKNOWN — never a truncated body ───────────────────────────────────
MAX_TOKEN_CEILING = 32000        # absolute stop for the doubling retry; a real answer never needs more here


def _input_fits(model, prompt, system):
    """(ok, detail) — does this payload fit the recorded input limit for (vendor, model)?

    UNMEASURED IS NOT UNLIMITED, and it is also not a reason to block: vendor_call.input_limit returns None
    when nobody has measured that model, and refusing every unmeasured model would make the guard useless on
    day one. So an unknown limit passes with the fact recorded, and a KNOWN limit is enforced. Counting the
    tokens of a fixed string is arithmetic, and comparing two numbers is arithmetic; nothing here is a
    judgement."""
    try:
        from . import vendor_call
        vendor = provider_for(model)
        raw = model.split(":", 1)[1] if ":" in model else model
        rec = vendor_call.input_limit(vendor, raw)
        # input_limit returns the whole RECORD ({max_chars, method, source, measured}), not a scalar. The
        # first cut of this did `int(lim)` on the dict, raised TypeError, and the except below reported
        # "input check unavailable" and PASSED — so the guard written to stop unchecked payloads silently
        # checked nothing, and a 506-token payload went out and billed. A swallow inside a guard is worse
        # than a swallow anywhere else: it disables the very thing whose presence the reader is trusting.
        lim = rec.get("max_chars") if isinstance(rec, dict) else rec
        if not lim:
            return True, "input limit UNMEASURED for this model"
        # CHARS, NOT TOKENS. record_input_limit's parameter is `max_chars`, because what it measures is a
        # ceiling the provider enforces on payload SIZE (measured on Moonshot by bisection). The first cut of
        # this compared a TOKEN count against that char limit and would have refused every payload about four
        # times too early — a guard that blocks correct work is not a safer guard, it is a broken one.
        n = len((system or "") + "\n" + (prompt or ""))
        if n > int(lim):
            return False, f"{n:,} chars exceeds the measured limit of {int(lim):,} for this model"
        return True, f"{n:,}/{int(lim):,} chars"
    except Exception as e:
        # The guard failing is not the payload failing, so the call proceeds — the vendor will reject an
        # oversized request itself, which is a worse error message but not a wrong answer. But it is said
        # OUT LOUD, because a silent "check unavailable" is indistinguishable from "checked and fine", and
        # that is exactly how this function passed a payload it was supposed to measure.
        from . import config as _cfg
        _cfg.warn_once(f"[spendguard] the input-size check FAILED for {model} ({type(e).__name__}: "
                       f"{str(e)[:60]}) — the payload was NOT checked. This is a bug in the check.")
        return True, f"input check unavailable ({type(e).__name__})"


def _call_guarded(model, prompt, max_tokens=None, sig=None, retries=2, **kw):
    """The input and output token guards, run on EVERY call. Not a helper anyone has to remember.

    An earlier version of this was a sibling function (`call_complete`) that callers opted into, and the
    result was measurable within the hour: 1 of 9 judging scripts used it, and a name-review re-run reported
    40 of 79 groups "UNREVIEWED" that were in fact just truncated. Safety a caller must remember to ask for
    is safety that will be missing exactly when the call matters. Both guards now live on the one path
    everything already takes.
    """
    from . import bulkgate
    ok, detail = _input_fits(model, prompt, kw.get("system"))
    if not ok:
        return {"provider": provider_for(model), "model": model, "text": None, "in_tok": 0, "out_tok": 0,
                "latency": 0.0, "cost": None, "finish_reason": None, "truncated": None,
                "error": f"payload too large: {detail} — split it rather than letting the vendor clip it"}
    if max_tokens is None:
        if not sig:
            raise ValueError(
                "call needs either an explicit max_tokens or a `sig` naming the call-class, so the budget "
                "can come from that class's measured p99 instead of a hand-picked literal. A literal is how "
                "truncated replies become silent wrong answers.")
        max_tokens = 0
    if sig:
        # A MEASURED BUDGET BEATS THE ARGUMENT. The caller's number is a guess about this call-class; the
        # class's own observed p99 is a measurement of it. Whichever is larger is used, so passing a literal
        # can only ever raise the floor, never cap the learned value back down.
        m = bulkgate.maxtokens(sig) or {}
        # COLD START IS HIGH, NOT LOW. This ended `or 2048` — so the FIRST call of any new class, the one
        # with no measured history, got a small cap chosen by nobody. That is the same defect in its
        # last hiding place: brand-new call-classes are exactly where an unexpectedly long answer arrives,
        # and 2048 quietly cut it. A high ceiling costs nothing — billing is per token GENERATED, so an
        # unused budget is free — while a low one destroys the answer. Once the class has been observed a
        # few times, `recommend` (its measured p99) takes over and the ceiling stops mattering.
        max_tokens = max(int(max_tokens or 0), int(m.get("recommend") or 0)) or MAX_TOKEN_CEILING
    attempt, budget = 0, int(max_tokens)
    while True:
        r = call(model, prompt, max_tokens=budget, _no_guard=True, **kw)
        if r.get("error"):
            return {**r, "truncated": None}                   # an errored call was not truncated, it failed
        trunc = bulkgate.is_truncated(r.get("finish_reason"), r.get("out_tok"), budget)
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
