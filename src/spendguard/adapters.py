"""Provider adapters for the `compare` harness.

Most providers expose an OpenAI-compatible API, so adding one is a single registry entry
(name, base_url, key env, model prefixes). Anthropic uses its own SDK. Add more at runtime
with register_provider(...). Calls go through the openai/anthropic SDKs, so the spend gate
already meters + budgets them.
"""
import time
import re
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


def call(model, prompt, max_tokens=512, system=None, reasoning=None):
    """Run one prompt against one model. Returns a result dict (never raises). `reasoning` (minimal|low|medium|high)
    sets reasoning effort for gpt-5/o-series reasoning models; defaults to 'minimal' for them (default-medium reasoning
    eats the token budget → empty output, and costs more — wrong for simple classify/extract calls)."""
    prov = provider_for(model)
    raw = model.split(":", 1)[1] if ":" in model else model
    spec = PROVIDERS[prov]
    base = {"provider": prov, "model": raw, "text": None, "in_tok": 0, "out_tok": 0, "latency": 0.0, "cost": None}
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
            c = anthropic.Anthropic(api_key=key)
            kw = {"model": raw, "max_tokens": max_tokens, "messages": [{"role": "user", "content": prompt}]}
            if system:
                kw["system"] = system
            m = c.messages.create(**kw)
            text = "".join(b.text for b in m.content if getattr(b, "type", None) == "text")
            in_tok, out_tok = m.usage.input_tokens, m.usage.output_tokens
        else:
            from openai import OpenAI
            c = OpenAI(api_key=key, base_url=spec["base_url"])
            msgs = ([{"role": "system", "content": system}] if system else []) + [{"role": "user", "content": prompt}]
            okw = {"model": raw, "messages": msgs}
            # gpt-5 / o-series are REASONING models: at default (medium) reasoning the token budget is spent on hidden
            # reasoning and the completion comes back EMPTY (+ costs more). For our simple classify/extract calls use
            # 'minimal' (the caller may override). Non-reasoning models reject the param → dropped on the retry below.
            if re.match(r"(gpt-5|o[134])", raw, re.I):
                okw["reasoning_effort"] = reasoning or "minimal"
            try:                                              # gpt-5+ require max_completion_tokens; older models take max_tokens
                r = c.chat.completions.create(max_completion_tokens=max_tokens, **okw)
            except Exception as e:
                if "reasoning_effort" in str(e):              # model doesn't accept it → drop + retry
                    okw.pop("reasoning_effort", None)
                    r = c.chat.completions.create(max_completion_tokens=max_tokens, **okw)
                elif "max_completion_tokens" in str(e) or "max_tokens" in str(e):
                    r = c.chat.completions.create(max_tokens=max_tokens, **okw)
                else:
                    raise
            text = r.choices[0].message.content
            in_tok, out_tok = r.usage.prompt_tokens, r.usage.completion_tokens
        dt = time.time() - t0
        try:
            cost = pricing.realtime_cost(raw, in_tok, out_tok)
        except Exception:
            cost = None  # model not in price table → shown as n/a
        return {**base, "text": text, "in_tok": in_tok, "out_tok": out_tok, "latency": dt, "cost": cost, "error": None}
    except Exception as e:
        return {**base, "latency": time.time() - t0, "error": str(e)[:140]}
