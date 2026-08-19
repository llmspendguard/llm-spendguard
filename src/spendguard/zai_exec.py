"""z.ai GLM Coding Plan lane — run GLM prompts on the flat-fee coding plan, not the metered z.ai API.

z.ai's GLM Coding Plan exposes an ANTHROPIC-COMPATIBLE endpoint that a coding-plan key draws against on the
flat monthly fee — the same shape as Claude Max (subscription_exec) and Codex (codex_exec). So the same lane
pattern applies: adapters.call routes a `zai/glm-*` prompt here at $0 BILLED (kind='subscription'), plan VALUE
tracked separately, and ANY failure cools the lane and falls back to the metered z.ai API — degrade, never break.

RAW HTTP ON PURPOSE — the same reason the other lanes shell out to a CLI. The spend gate patches the anthropic
and openai SDK clients, so calling the SDK in-process would make the gate record this plan call as a METERED
realtime charge (measured: glm-5.3 booked UNPRICED) ON TOP OF the $0 subscription row adapters writes — a phantom
double-record of a call that cost nothing. A stdlib urllib POST is invisible to the SDK patch, so the plan call
is recorded once, as $0 subscription, and never mistaken for metered spend. (This is exactly why the CLI lanes
are subprocesses: a subscription path must not travel the gated SDK.)

DISTINCT FROM THE METERED `zai` PROVIDER. PROVIDERS['zai'] is the per-token API (paas/v4 + ZAI_API_KEY). This
lane is the coding PLAN: a different endpoint (api/anthropic) drawing on the flat plan. Same account key works.

Setup (docs.z.ai/devpack/tool/claude): keys.env ZAI_CODING_API_KEY, else the account's ZAI_API_KEY is used.
"""
import json
import time
import urllib.request

# The coding-plan Anthropic-compatible endpoint (docs.z.ai/devpack/tool/claude). A named default with
# provenance, overridable via config for a future region/endpoint change — never a literal scattered through
# the call path. `_ANTHROPIC_VERSION` is the REST contract's required header (mirrors vendor_call._ANTHROPIC_VERSION).
DEFAULT_BASE_URL = "https://api.z.ai/api/anthropic"
_ANTHROPIC_VERSION = "2023-06-01"
KEY_ENV = "ZAI_CODING_API_KEY"          # the coding-PLAN token; falls back to the account's ZAI_API_KEY
TIMEOUT_S = 300
# Only used when neither the caller nor the price table knows the model's output ceiling. The plan is flat-fee,
# so a generous cap costs nothing and only avoids truncation; a real measured/published max always wins over it.
_FALLBACK_MAX_TOKENS = 16384


def _base_url():
    from . import config
    try:
        return str(config._cfg_get("zai_coding", "base_url", DEFAULT_BASE_URL) or DEFAULT_BASE_URL)
    except Exception:
        return DEFAULT_BASE_URL


def _key():
    from . import config
    # Prefer the explicit coding-plan token. Fall back to the account's z.ai key: on an ACTIVE GLM Coding Plan
    # the same account key draws on the plan when it hits the coding endpoint. This is safe for accounting — a
    # key with NO plan gets an auth/quota error from api/anthropic (metered billing lives on the separate
    # paas/v4 endpoint), so adapters cools the lane and falls back to the metered API; a metered charge can
    # never be silently booked as $0. Set ZAI_CODING_API_KEY explicitly to override.
    return config.api_key(KEY_ENV) or config.api_key("ZAI_API_KEY")


def available() -> bool:
    """The lane can run iff a plan-capable key is resolvable. Mirrors the CLI lanes' probe, but the resource
    here is a key + endpoint, not a host binary."""
    return bool(_key())


def run_prompt(prompt, system=None, model=None, timeout=TIMEOUT_S, reasoning=None):   # reasoning: protocol-uniform; accepted, not yet applied on this lane
    """→ {text, in_tok, out_tok, latency, error} from ONE plan-billed GLM completion over the Anthropic-
    compatible coding endpoint, via RAW HTTP so the spend gate never meters it. `model` = the glm id the caller
    asked for (glm-5.3 etc.), passed through. Same typed contract as the CLI lanes."""
    key = _key()
    if not key:
        return {"error": f"no z.ai key ({KEY_ENV} or ZAI_API_KEY) — add it to keys.env"}
    mdl = (model or "").split(":", 1)[-1] or "glm-5.3"    # newest flagship on the plan; caller may override
    try:
        from . import pricing
        mt = int(pricing.max_output(mdl) or 0) or _FALLBACK_MAX_TOKENS
    except Exception:
        mt = _FALLBACK_MAX_TOKENS
    body = {"model": mdl, "max_tokens": mt, "messages": [{"role": "user", "content": prompt}]}
    if system:
        body["system"] = system
    from . import config
    req = urllib.request.Request(
        _base_url().rstrip("/") + "/v1/messages", data=json.dumps(body).encode("utf-8"),
        headers={"x-api-key": key, "anthropic-version": _ANTHROPIC_VERSION, "content-type": "application/json"})
    t0 = time.time()
    try:
        resp = urllib.request.urlopen(req, context=config.ssl_context(), timeout=timeout)
        d = json.loads(resp.read())
    except Exception as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8", "ignore")[:200]      # HTTPError carries the provider's 4xx body
        except Exception:
            pass
        return {"error": (f"{type(e).__name__}: {str(e)[:100]}" + (f" — {detail}" if detail else "")),
                "latency": time.time() - t0}
    text = "".join(b.get("text", "") for b in (d.get("content") or []) if b.get("type") == "text")
    u = d.get("usage") or {}
    return {"text": text, "in_tok": int(u.get("input_tokens") or 0),
            "out_tok": int(u.get("output_tokens") or 0), "latency": time.time() - t0, "error": None}
