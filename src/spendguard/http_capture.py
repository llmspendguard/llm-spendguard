"""Raw-HTTP capture — spend that bypasses the SDKs becomes VISIBLE (never blocked).

The gate patches SDK surfaces; a `curl`-style `httpx`/`requests` call straight at a provider API in a
gated venv used to be completely invisible (not estimated, not recorded, and — for realtime — not even
reconcilable without an admin key). This layer patches the HTTP clients themselves for KNOWN provider
hosts and, capture-first and strictly FAIL-OPEN:

  • parses usage out of KNOWN response shapes (chat/completions, responses, messages, embeddings)
    → records into the SAME realtime ledger as the SDK gate;
  • anything else to a provider host → a LOUD `raw_http_unmetered` audit event (feeds coverage/leak
    thinking: UNKNOWN spend stays visible, never $0-clean).

It never blocks and never alters a request or response — enforcement stays at the SDK layer; this
layer exists so raw calls can't be SILENT. Double-count safety: the SDKs run on httpx underneath, so
every gated SDK wrapper sets a ContextVar while the real call runs and this layer skips those.

Knob: gate.http_capture = on|off (env SPENDGUARD_HTTP_CAPTURE, default on).
"""
import contextvars
import json
import os
import sys

# set by the SDK gate wrappers around the real call — HTTP-level capture skips SDK-originated traffic
in_sdk_call = contextvars.ContextVar("spendguard_in_sdk_call", default=False)

PROVIDER_HOSTS = {
    "api.openai.com": "openai",
    "api.anthropic.com": "anthropic",
    "generativelanguage.googleapis.com": "google",
}
_warned_paths = set()


def _enabled():
    v = os.environ.get("SPENDGUARD_HTTP_CAPTURE")
    if v is not None:
        return v.strip().lower() not in ("0", "off", "false", "no")
    try:
        from . import config
        return str(config._cfg_get("gate", "http_capture", "on")).lower() != "off"
    except Exception:
        return True


def _usage_from_json(body):
    """(model, in_tok, out_tok) from a known provider response body, else None. Shapes: OpenAI chat
    (usage.prompt/completion_tokens), Responses + Anthropic (usage.input/output_tokens), embeddings
    (usage.prompt_tokens, out=0). Mechanical field reads — no meaning decided here."""
    if not isinstance(body, dict):
        return None
    u = body.get("usage")
    model = body.get("model") or ""
    if not isinstance(u, dict):
        return None
    if "prompt_tokens" in u:
        return model, int(u.get("prompt_tokens") or 0), int(u.get("completion_tokens") or 0)
    if "input_tokens" in u or "output_tokens" in u:
        return model, int(u.get("input_tokens") or 0), int(u.get("output_tokens") or 0)
    return None


def _capture(host, path, status, body_bytes):
    """Record one raw provider response. Known usage shape → realtime ledger; else a loud unmetered event."""
    try:
        provider = PROVIDER_HOSTS.get(host)
        if provider is None or status is None or int(status) >= 400:
            return
        got = None
        try:
            got = _usage_from_json(json.loads(body_bytes))
        except Exception:
            got = None
        from . import gate
        if got and (got[1] or got[2]):
            model, i, o = got
            gate._record_rt(model, {"model": model, "raw_http": True}, i, o, provider=provider)
            return
        key = (host, path.split("?")[0])
        gate._log({"kind": "raw_http_unmetered", "provider": provider, "host": host,
                   "path": key[1], "decision": "recorded_unmetered"})
        if key not in _warned_paths:
            _warned_paths.add(key)
            print(f"[spend_gate] WARN raw HTTP call to {host}{key[1]} carried no parseable usage — "
                  f"logged UNMETERED (spend invisible until reconcile; prefer the SDK so it's gated)",
                  file=sys.stderr)
    except Exception:
        pass                                          # capture must never affect the caller


def _wrap_httpx_send(orig, is_async):
    import functools
    if is_async:
        @functools.wraps(orig)
        async def w(self, request, **kw):
            r = await orig(self, request, **kw)
            if _enabled() and not in_sdk_call.get():
                try:
                    if request.url.host in PROVIDER_HOSTS and not kw.get("stream", False):
                        await r.aread()
                        _capture(request.url.host, request.url.path, r.status_code, r.content)
                except Exception:
                    pass
            return r
    else:
        @functools.wraps(orig)
        def w(self, request, **kw):
            r = orig(self, request, **kw)
            if _enabled() and not in_sdk_call.get():
                try:
                    if request.url.host in PROVIDER_HOSTS and not kw.get("stream", False):
                        r.read()
                        _capture(request.url.host, request.url.path, r.status_code, r.content)
                except Exception:
                    pass
            return r
    w._spend_gated = True
    return w


def _wrap_requests_send(orig):
    import functools

    @functools.wraps(orig)
    def w(self, request, **kw):
        r = orig(self, request, **kw)
        if _enabled() and not in_sdk_call.get():
            try:
                from urllib.parse import urlparse
                u = urlparse(request.url or "")
                if u.hostname in PROVIDER_HOSTS and not kw.get("stream", False):
                    _capture(u.hostname, u.path, r.status_code, r.content)
            except Exception:
                pass
        return r
    w._spend_gated = True
    return w


def install() -> bool:
    """Patch httpx (sync+async) and requests transports, idempotently and only if already importable.
    Returns True iff at least one client is now wrapped."""
    wired = False
    try:
        import httpx
        if not getattr(httpx.Client.send, "_spend_gated", False):
            httpx.Client.send = _wrap_httpx_send(httpx.Client.send, False)
        if not getattr(httpx.AsyncClient.send, "_spend_gated", False):
            httpx.AsyncClient.send = _wrap_httpx_send(httpx.AsyncClient.send, True)
        wired = True
    except Exception:
        pass
    try:
        import requests
        if not getattr(requests.Session.send, "_spend_gated", False):
            requests.Session.send = _wrap_requests_send(requests.Session.send)
        wired = True
    except Exception:
        pass
    return wired
