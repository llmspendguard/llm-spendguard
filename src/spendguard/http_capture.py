"""Raw-HTTP capture — spend that bypasses the SDKs becomes VISIBLE (never blocked).

The gate patches SDK surfaces; a `curl`-style `httpx`/`requests`/stdlib-`urllib` call straight at a
provider API in a gated venv used to be completely invisible (not estimated, not recorded, and — for
realtime — not even reconcilable without an admin key). This layer patches the HTTP clients themselves
for KNOWN provider hosts and, capture-first and strictly FAIL-OPEN:

  • parses usage out of KNOWN response shapes (chat/completions, responses, messages, embeddings)
    → records into the SAME realtime ledger as the SDK gate;
  • anything else to a provider host → a LOUD `raw_http_unmetered` audit event (feeds coverage/leak
    thinking: UNKNOWN spend stays visible, never $0-clean).

It never blocks and never alters a request or response — enforcement stays at the SDK layer; this
layer exists so raw calls can't be SILENT. Double-count safety: the SDKs run on httpx underneath, so
every gated SDK wrapper sets a ContextVar while the real call runs and this layer skips those. The
stdlib `urllib` patch (the SDKs never use urllib) hooks OpenerDirector.open — the single chokepoint every
`urlopen()` / `build_opener().open()` funnels through, so `from urllib.request import urlopen` callers are
covered too — and captures raw provider POSTs only (token spend is always a POST; GET reads never bill).
urllib's HTTPResponse is read-once (httpx/requests buffer; urllib does not), so it RE-SERVES the body on
the SAME response object and the caller still receives it unchanged.

FAIL-OPEN, WITH ONE EXCEPTION: a genuine malfunction is swallowed (capture must never break the caller's
already-completed call), but a DELIBERATE stop — a spend refusal / dispatch deadline
(gate.deliberate_stop_types) — is RE-RAISED, never downgraded to 'keep going' (the [deliberate-refusal
never fail-open] doctrine).

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

# Bound the buffered read of a urllib response: big enough for a large embeddings batch, small enough that a
# pathological / oversized provider response cannot OOM the host. A body whose Content-Length exceeds this (or is
# unknown/chunked) is NOT drained — it is logged visible instead, so capture never trades a hang/OOM for a row.
_MAX_CAPTURE_BYTES = 64 * 1024 * 1024


def _enabled():
    v = os.environ.get("SPENDGUARD_HTTP_CAPTURE")
    if v is not None:
        return v.strip().lower() not in ("0", "off", "false", "no")
    try:
        from . import config
        return str(config._cfg_get("gate", "http_capture", "on")).lower() != "off"
    except Exception:
        return True


def _is_deliberate_stop(e):
    """True iff `e` is a DELIBERATE stop — a spend refusal or dispatch deadline (gate.deliberate_stop_types). Such
    an exception must PROPAGATE out of a fail-open capture handler, never be swallowed as if it were a malfunction
    (the [deliberate-refusal never fail-open] doctrine — the CONCEPT, so a new refusal subclass is covered without
    editing an enumerated tuple). A genuine malfunction returns False → the caller swallows it so a bookkeeping
    hiccup never breaks the user's already-completed call."""
    try:
        from . import gate
        return isinstance(e, gate.deliberate_stop_types())
    except Exception:
        return False


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
    except Exception as e:
        if _is_deliberate_stop(e):                        # a spend refusal must surface, never be swallowed here
            raise
        # a genuine malfunction is swallowed — capture must never affect the caller


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
                except Exception as e:
                    if _is_deliberate_stop(e):
                        raise
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
                except Exception as e:
                    if _is_deliberate_stop(e):
                        raise
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
            except Exception as e:
                if _is_deliberate_stop(e):
                    raise
        return r
    w._spend_gated = True
    return w


def _urllib_target(fullurl, data):
    """(url, is_post) from OpenerDirector.open's (fullurl, data). `fullurl` is a str OR a urllib.request.Request.
    A token-billing call is ALWAYS a POST (a prompt is sent); GETs to a provider host are metadata/reconciliation
    reads (list models/batches, read usage) that never bill — so capturing only POSTs keeps them out with no false
    'unmetered' noise and needs no cooperation from those callers. Mechanical extraction — no meaning decided."""
    if hasattr(fullurl, "get_full_url"):                 # a urllib.request.Request
        url = fullurl.get_full_url()
        try:
            method = (fullurl.get_method() or "").upper()
        except Exception:
            method = "POST" if (getattr(fullurl, "data", None) is not None or data is not None) else "GET"
    else:
        url = fullurl or ""
        method = "POST" if data is not None else "GET"
    return url, (method == "POST")


def _capture_urllib(fullurl, data, resp):
    """Capture one raw stdlib-urllib provider response WITHOUT consuming it for the caller.

    urllib returns a read-once http.client.HTTPResponse (httpx/requests buffer; urllib does not), so we read the
    body then RE-SERVE the same bytes from a BytesIO on the response object — the caller's later .read()/.json()/
    iteration see the full body unchanged, and every other HTTPResponse method/attribute is preserved. Only a
    BOUNDED, non-streaming body from a billable POST to a known provider host is read: a stream (SSE), an oversized
    body, or an unknown-length (chunked) body is left UNTOUCHED and logged VISIBLE (never silently $0), so capture
    can never OOM, hang, or half-consume the caller. A read that fails is logged too — a billable call is never
    dropped in silence."""
    from urllib.parse import urlparse
    url, is_post = _urllib_target(fullurl, data)
    host = urlparse(url).hostname
    if host not in PROVIDER_HOSTS or not is_post:        # only billable POSTs to a known provider host
        return
    try:
        from . import budget
        if budget.is_reading_history():                  # a history read is not spend (reconcile/oracle downloads)
            return
    except Exception:
        pass
    path = urlparse(url).path.split("?")[0]
    status = getattr(resp, "status", None)
    if status is None:
        status = getattr(resp, "code", None)
    ctype, clen = "", None
    try:
        h = getattr(resp, "headers", None)
        if h is not None:
            ctype = (h.get("Content-Type", "") or "").lower()
            cl = h.get("Content-Length")
            clen = int(cl) if cl not in (None, "") else None
    except Exception:
        pass
    from . import gate
    # Whether a response is safe to read for usage is decided MECHANICALLY by its declared size — never by
    # classifying the content-type. A body is buffered only when it has a Content-Length within the cap; anything
    # else (a stream, which carries no Content-Length whether it is SSE or ndjson; an oversized body; or an
    # unknown length) is left UNDRAINED and logged VISIBLE, with the raw content-type recorded as CONTEXT (not a
    # decision). So capture never OOMs, hangs, or half-consumes the caller, and never records a $0-silent drop.
    if clen is None:
        reason = "recorded_unmetered_unbuffered"        # no Content-Length → a stream / chunked body: don't drain
    elif clen > _MAX_CAPTURE_BYTES:
        reason = "recorded_unmetered_oversized"
    else:
        reason = None
    if reason is not None:
        gate._log({"kind": "raw_http_unmetered", "provider": PROVIDER_HOSTS[host], "host": host,
                   "path": path, "content_type": ctype or None, "decision": reason})
        return
    try:
        body = resp.read()                               # bounded by the declared Content-Length (<= cap): a safe read
    except Exception as e:
        if _is_deliberate_stop(e):
            raise
        # a failed read must not silently drop a billable call — keep it VISIBLE (never counted as free).
        gate._log({"kind": "raw_http_unmetered", "provider": PROVIDER_HOSTS[host], "host": host,
                   "path": path, "decision": "recorded_unmetered_read_failed", "error": type(e).__name__})
        return
    import io
    buf = io.BytesIO(body)
    for _m in ("read", "readline", "readlines", "readinto"):   # re-serve the SAME bytes to the caller (read-once fix)
        try:
            setattr(resp, _m, getattr(buf, _m))
        except Exception:
            pass
    _capture(host, path, status, body)


def _wrap_urllib_open(orig):
    """Wrap urllib.request.OpenerDirector.open — the single chokepoint `urlopen()` and `build_opener().open()`
    both funnel through, so ALL raw-urllib callers are covered (patching `urlopen` alone misses a
    `from urllib.request import urlopen` binding and every custom opener). Do NOT also patch `urlopen`, or a call
    is captured twice."""
    import functools

    @functools.wraps(orig)
    def w(self, fullurl, data=None, *args, **kw):
        resp = orig(self, fullurl, data, *args, **kw)
        if _enabled() and not in_sdk_call.get():
            try:
                _capture_urllib(fullurl, data, resp)
            except Exception as e:
                if _is_deliberate_stop(e):               # a spend refusal must surface, never be swallowed here
                    raise
                # a malfunction in capture must never affect the caller's already-completed call
        return resp
    w._spend_gated = True
    return w


def install() -> bool:
    """Patch httpx (sync+async), requests, and stdlib urllib transports, idempotently and only if already
    importable (urllib is stdlib, so it always wires). Returns True iff at least one client is now wrapped."""
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
    try:
        import urllib.request                            # stdlib — always importable; OpenerDirector.open is the ONE
        if not getattr(urllib.request.OpenerDirector.open, "_spend_gated", False):   # chokepoint (urlopen calls it)
            urllib.request.OpenerDirector.open = _wrap_urllib_open(urllib.request.OpenerDirector.open)
        wired = True
    except Exception:
        pass
    return wired
