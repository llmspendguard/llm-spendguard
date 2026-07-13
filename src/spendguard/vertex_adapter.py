"""Google Gemini / Vertex AI coverage — direct google-genai SDK (for teams NOT routing through LiteLLM). Patches
`google.genai` Models.generate_content AND Models.embed_content (+ async), recording usage into the SAME realtime
ledger as the SDK gate, labelled provider='google'. CAPTURE-focused + strictly FAIL-OPEN: the real call runs
untouched.

Opt-in: `spendguard.install_vertex()` after importing the SDK (heavy/optional — the startup gate only auto-wires it
if `google.genai` is already imported)."""
import functools
import sys


def _toks(result):
    """(in_tok, out_tok) from a GenerateContentResponse.usage_metadata, else None (→ nothing recorded)."""
    um = getattr(result, "usage_metadata", None)
    if um is None:
        return None
    return int(getattr(um, "prompt_token_count", None) or 0), int(getattr(um, "candidates_token_count", None) or 0)


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


def _capture(kw, result, toks_fn=_toks):
    try:
        from . import gate
        model = (kw or {}).get("model") or getattr(result, "model_version", "") or ""
        u = toks_fn(result)
        if u and (u[0] or u[1]):
            gate._record_rt(model, {"model": model}, u[0], u[1], provider="google")
    except Exception as e:
        print(f"[spend_gate] WARN vertex capture failed ({e}); call unaffected", file=sys.stderr)


def _wrap(orig, is_async, toks_fn=_toks):
    if is_async:
        @functools.wraps(orig)
        async def w(self, *a, **kw):
            r = await orig(self, *a, **kw)                # real call untouched; errors propagate
            _capture(kw, r, toks_fn)
            return r
    else:
        @functools.wraps(orig)
        def w(self, *a, **kw):
            r = orig(self, *a, **kw)
            _capture(kw, r, toks_fn)
            return r
    w._spend_gated = True
    return w


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
    for cls_name, is_async in (("Models", False), ("AsyncModels", True)):
        cls = getattr(gm, cls_name, None)
        if cls is None:
            continue
        for method, toks_fn in (("generate_content", _toks), ("embed_content", _embed_toks)):
            cur = getattr(cls, method, None)
            if cur is None:
                continue
            if getattr(cur, "_spend_gated", False):
                wired = True
                continue
            setattr(cls, method, _wrap(cur, is_async, toks_fn))
            wired = True
    return wired
