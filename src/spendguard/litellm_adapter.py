"""LiteLLM coverage — capture spend for ANY provider routed through LiteLLM (Bedrock, Vertex/Gemini, Cohere,
Mistral, …) via LiteLLM's native success-callback, NOT monkeypatching. Recorded into the SAME realtime ledger the
SDK gate uses (`gate._record_rt`), so LiteLLM spend rolls up, attributes, and reconciles identically. Priced through
`pricing.py` like everything else (never hardcoded).

WHY a callback, not the gate monkeypatch: LiteLLM has its own per-provider HTTP handlers, so the openai/anthropic
SDK patches don't see most LiteLLM traffic — that's the coverage hole. The exception is the providers LiteLLM serves
THROUGH the OpenAI SDK (openai, azure): those are ALREADY captured by the SDK gate, so we skip them here to avoid
double-counting. Net: the SDK gate covers direct openai/anthropic; this covers everything else LiteLLM normalizes.

Activation: opt-in one-liner for LiteLLM users — `import spendguard; spendguard.install_litellm()` after importing
litellm (it's heavy + optional, so the startup gate does NOT force-import it; it only auto-wires if litellm is
already loaded). The proxy deployment integrates at the proxy's callback config instead (Enterprise)."""
import sys

# Providers LiteLLM routes through the OpenAI SDK (which the gate already patches) → skip, or we'd count them twice.
_SDK_GATED = {"openai", "azure", "azure_ai", "azure_text"}


def _usage(response_obj):
    """(prompt_tokens, completion_tokens) from a LiteLLM ModelResponse (pydantic-like) or a dict. Best-effort → (0,0)."""
    u = getattr(response_obj, "usage", None)
    if u is None and isinstance(response_obj, dict):
        u = response_obj.get("usage")
    if u is None:
        return 0, 0
    get = (u.get if isinstance(u, dict) else (lambda k: getattr(u, k, None)))
    return int(get("prompt_tokens") or 0), int(get("completion_tokens") or 0)


def record_litellm(kwargs, response_obj, start_time=None, end_time=None):
    """LiteLLM success-callback signature `(kwargs, response, start, end)`. Records one completion's usage into the
    realtime ledger. FAIL-OPEN — it's a post-success logger and must never break the user's LiteLLM call."""
    try:
        from . import gate
        prov = str((kwargs or {}).get("custom_llm_provider") or "").lower()
        if prov in _SDK_GATED:
            return                                  # already captured by the OpenAI-SDK gate → no double count
        model = (kwargs or {}).get("model") or getattr(response_obj, "model", "") or ""
        in_tok, out_tok = _usage(response_obj)
        if not (in_tok or out_tok):
            return                                  # nothing to record (e.g. an embeddings/stream event w/o usage here)
        # Prefer LiteLLM's OWN computed cost — it maintains an accurate cross-provider price table, so Bedrock/Vertex/
        # Cohere/… are priced even when our prices.json doesn't carry them. Falls back to our pricing (cost=None) for
        # models LiteLLM didn't cost. Label the ledger with LiteLLM's provider so the row reconciles to the right place.
        cost = (kwargs or {}).get("response_cost")
        try:
            cost = float(cost) if cost is not None else None
        except (TypeError, ValueError):
            cost = None
        gate._record_rt(model, {"model": model}, in_tok, out_tok, cost=cost, provider=prov or None)
    except Exception as e:
        print(f"[spend_gate] WARN litellm capture failed ({e}); call unaffected", file=sys.stderr)


def install(force: bool = False) -> bool:
    """Register the recorder on LiteLLM's success callback (idempotent). `force=True` imports litellm (the explicit
    `spendguard.install_litellm()` path); otherwise only wires if litellm is ALREADY imported (so the startup gate
    never force-imports a heavy optional dep). Returns True iff litellm is present and now wired."""
    lm = sys.modules.get("litellm")
    if lm is None:
        if not force:
            return False
        try:
            import litellm as lm
        except Exception:
            return False
    cbs = list(getattr(lm, "success_callback", None) or [])
    if record_litellm not in cbs:
        lm.success_callback = cbs + [record_litellm]   # litellm fires this for sync AND async completions
    return True
