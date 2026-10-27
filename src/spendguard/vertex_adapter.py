"""Google Gemini / Vertex AI coverage — direct google-genai SDK (for teams NOT routing through LiteLLM). Patches
`google.genai` Models.generate_content AND Models.embed_content (+ async), recording usage into the SAME realtime
ledger as the SDK gate, labelled provider='google'. CAPTURE-focused + strictly FAIL-OPEN: the real call runs
untouched.

Opt-in: `spendguard.install_vertex()` after importing the SDK (heavy/optional — the startup gate only auto-wires it
if `google.genai` is already imported)."""
import functools
import sys


_VERTEX_CAPTURE_METHODS = ("generate_content", "embed_content")
_VERTEX_CAPTURE_CLASSES = (("Models", False), ("AsyncModels", True))


def _toks(result):
    """(in_tok, out_tok) from a GenerateContentResponse.usage_metadata, else None (→ nothing recorded)."""
    um = getattr(result, "usage_metadata", None)
    if um is None:
        return None
    candidate_count = getattr(um, "candidates_token_count", None)
    return int(getattr(um, "prompt_token_count", None) or 0), int(candidate_count) if candidate_count is not None else None


def _embed_toks(result):
    """(in_tok, 0) for an EmbedContentResponse. Token counts live per-embedding (statistics.token_count) on the
    new SDK, or on a top-level usage/metadata for older shapes; embeddings have no output tokens. None → skip."""
    embs = getattr(result, "embeddings", None)
    if embs:
        n = 0
        for e in embs:
            st = getattr(e, "statistics", None)
            n += int(getattr(st, "token_count", None) or 0) if st is not None else 0
        if n:
            return n, 0
    um = getattr(result, "usage_metadata", None)          # some shapes report a flat prompt count instead
    if um is not None:
        n = int(getattr(um, "prompt_token_count", None) or getattr(um, "total_token_count", None) or 0)
        if n:
            return n, 0
    return None


def _capture_vertex_result(kw, result, toks_fn=_toks):
    try:
        from . import gate
        model = (kw or {}).get("model") or getattr(result, "model_version", "") or ""
        u = toks_fn(result)
        if u is not None:
            gate._record_rt(model, {"model": model}, u[0], u[1], provider="google")
    except Exception as e:
        print(f"[spend_gate] WARN vertex capture failed ({e}); call unaffected", file=sys.stderr)


def _capture_vertex_usage_wrapper(orig, is_async, toks_fn=_toks):
    if is_async:
        @functools.wraps(orig)
        async def w(self, *a, **kw):
            r = await orig(self, *a, **kw)                # real call untouched; errors propagate
            _capture_vertex_result(kw, r, toks_fn)
            return r
    else:
        @functools.wraps(orig)
        def w(self, *a, **kw):
            r = orig(self, *a, **kw)
            _capture_vertex_result(kw, r, toks_fn)
            return r
    w._spend_gated = True
    return w


def vertex_adapter_is_wired() -> bool:
    """True only when at least one imported google-genai spend surface is actually wrapped."""
    models_module = sys.modules.get("google.genai.models")
    if models_module is None:
        return False
    found_surface = False
    for class_name, _is_async in _VERTEX_CAPTURE_CLASSES:
        models_class = getattr(models_module, class_name, None)
        if models_class is None:
            continue
        for method_name in _VERTEX_CAPTURE_METHODS:
            method = getattr(models_class, method_name, None)
            if method is None:
                continue
            found_surface = True
            if not getattr(method, "_spend_gated", False):
                return False
    return found_surface


def install(force: bool = False) -> bool:
    """Patch google-genai generate_content + embed_content (sync + async) to capture usage (idempotent). Only
    wires if the SDK is already imported, unless `force`. Returns True iff at least one method is now patched."""
    if not force and sys.modules.get("google.genai") is None:
        return False
    try:
        from google.genai import models as gm
    except Exception:
        return False
    wired = False
    for cls_name, is_async in _VERTEX_CAPTURE_CLASSES:
        cls = getattr(gm, cls_name, None)
        if cls is None:
            continue
        for method, toks_fn in ((_VERTEX_CAPTURE_METHODS[0], _toks), (_VERTEX_CAPTURE_METHODS[1], _embed_toks)):
            cur = getattr(cls, method, None)
            if cur is None:
                continue
            if getattr(cur, "_spend_gated", False):
                wired = True
                continue
            setattr(cls, method, _capture_vertex_usage_wrapper(cur, is_async, toks_fn))
            wired = True
    return wired and vertex_adapter_is_wired()
