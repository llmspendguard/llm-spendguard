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
import random
import time
import urllib.error
import urllib.request

# 429 BACKOFF (the z.ai plan's concurrency is DYNAMIC + burst-sensitive — measured Max ~12 concurrent, 429 beyond).
# A burst 429 is transient: retry the SAME request with exponential backoff + jitter and it clears, instead of a
# hard lane miss spilling every over-cap call to the metered API. Operational tunables (like the timeouts), named
# here — never a magic literal at the call site.
_ZAI_MAX_429_RETRIES = 3
_ZAI_BACKOFF_BASE_S = 0.6
_ZAI_BACKOFF_JITTER_S = 0.4

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


def _output_budget(mdl):
    """max_tokens for a glm call, via the SAME shared resolver the metered path uses (pricing.output_ceiling), so
    this lane cannot DRIFT from _call_guarded's authority order: published limits cache → live /models → the learned
    max_output fact → backstop. A lane cannot retry-heal (run_prompt takes no max_tokens and there is no downward
    halving), so it passes learned_floor=_FALLBACK_MAX_TOKENS: a poisoned-low fact (the auto-heal 2000/7 class) is
    floored and cannot truncate this lane, and an unknown model falls to the same backstop."""
    from . import pricing
    return int(pricing.output_ceiling("zai", mdl, _FALLBACK_MAX_TOKENS, learned_floor=_FALLBACK_MAX_TOKENS))


def run_prompt(prompt, system=None, model=None, timeout=TIMEOUT_S, reasoning=None):
    """→ {text, in_tok, out_tok, latency, error} from ONE plan-billed GLM completion over the Anthropic-
    compatible coding endpoint, via RAW HTTP so the spend gate never meters it. `model` = the glm id the caller
    asked for (glm-5.3 etc.), passed through. Same typed contract as the CLI lanes.

    `reasoning` engages GLM's extended THINKING via the endpoint's Anthropic-shape `thinking` block, sized by a
    MEASURED budget (models.thinking_budget — never a guessed fraction; no measured fact → no thinking). Fail-safe:
    if the enriched call fails, it retries ONCE WITHOUT thinking (thinking is the one optional enrichment here) —
    so a lane whose endpoint/model does not accept the param never breaks, it degrades to a plain completion. The
    text extraction keeps only text blocks, so any returned `thinking` blocks are naturally ignored."""
    key = _key()
    if not key:
        return {"error": f"no z.ai key ({KEY_ENV} or ZAI_API_KEY) — add it to keys.env"}
    mdl = (model or "").split(":", 1)[-1] or "glm-5.3"    # newest flagship on the plan; caller may override
    try:
        mt = _output_budget(mdl)
    except Exception as e:
        from . import gate as _g
        if _g.is_deliberate_stop(e):
            raise                                         # a deliberate stop (refusal/deadline) must PROPAGATE, not be masked
        mt = _FALLBACK_MAX_TOKENS
    body = {"model": mdl, "max_tokens": mt, "messages": [{"role": "user", "content": prompt}]}
    if system:
        body["system"] = system
    from . import config, models as _mdl
    _tb = _mdl.thinking_budget(mdl, reasoning, mt) if reasoning else None
    if _tb:
        body["thinking"] = {"type": "enabled", "budget_tokens": _tb}

    def _post(b):
        req = urllib.request.Request(
            _base_url().rstrip("/") + "/v1/messages", data=json.dumps(b).encode("utf-8"),
            headers={"x-api-key": key, "anthropic-version": _ANTHROPIC_VERSION, "content-type": "application/json"})
        # CLOSE the response — `with` releases the socket/fd back to the pool on EVERY call. Leaving it open
        # (the prior `resp = urlopen(...); resp.read()`) leaked a connection per call, so a burst of concurrent
        # lane calls exhausted fds and hung. urllib gives no pooling, so an unclosed handle is a hard leak.
        with urllib.request.urlopen(req, context=config.ssl_context(), timeout=timeout) as resp:
            return json.loads(resp.read())

    def _post_ratelimited(b):
        """POST with exponential backoff + jitter on HTTP 429 (the plan's dynamic burst/concurrency cap). Retries
        the SAME request so an over-cap burst clears instead of spilling to the metered API. 429 is read from the
        STRUCTURED HTTPError.code (a status field), NEVER from error prose. Any non-429 error propagates at once
        for the caller's fail-safe (thinking-strip) to handle."""
        for attempt in range(_ZAI_MAX_429_RETRIES + 1):
            try:
                return _post(b)
            except urllib.error.HTTPError as he:
                if he.code == 429 and attempt < _ZAI_MAX_429_RETRIES:
                    time.sleep(_ZAI_BACKOFF_BASE_S * (2 ** attempt) + random.uniform(0, _ZAI_BACKOFF_JITTER_S))
                    continue
                raise

    def _errdict(exc):
        detail = ""
        try:
            detail = exc.read().decode("utf-8", "ignore")[:200]    # HTTPError carries the provider's 4xx body
        except Exception:
            pass
        return {"error": (f"{type(exc).__name__}: {str(exc)[:100]}" + (f" — {detail}" if detail else "")),
                "latency": time.time() - t0}

    t0 = time.time()
    try:
        d = _post_ratelimited(body)
    except Exception as e:
        # FAIL-SAFE: the enriched request failed. thinking is the one OPTIONAL enrichment, so retry ONCE without it
        # (no error-prose classification — an anthropic-shape 4xx names the reason only in free text, and reading
        # prose to DECIDE is a meaning-judgement we don't make on a hot lane path). If the plain retry also fails,
        # the failure was not the thinking param → return it. Nothing to relax (no thinking) → return the error.
        if "thinking" not in body:
            return _errdict(e)
        try:
            d = _post_ratelimited({k: v for k, v in body.items() if k != "thinking"})
        except Exception as e2:
            return _errdict(e2)
    text = "".join(b.get("text", "") for b in (d.get("content") or []) if b.get("type") == "text")
    u = d.get("usage") or {}
    return {"text": text, "in_tok": int(u.get("input_tokens") or 0),
            "out_tok": int(u.get("output_tokens") or 0), "latency": time.time() - t0, "error": None}
