"""Emit spendguard events to whatever observability you already run — spendguard stays the
*enforcement* layer, not another dashboard.

Sinks (all optional, all best-effort, none ever block or break the gate):
  - in-process callback:  spendguard.on_event(fn)   # fn(event: dict)
  - webhook:              POST JSON to a URL          # config emit.webhook / $SPENDGUARD_WEBHOOK
  - OpenTelemetry:        GenAI-conventioned metrics + spans  # config emit.otel=true / $SPENDGUARD_OTEL

The OTel sink emits using OpenTelemetry **GenAI semantic conventions** (gen_ai.system /
gen_ai.request.model / gen_ai.operation.name / gen_ai.usage.*tokens + a spendguard.cost_usd metric),
to the GLOBAL meter+tracer. Point your own OTel SDK's OTLP exporter at whatever you run — **Langfuse,
Helicone, Arize Phoenix, Honeycomb** all ingest OTLP — and spendguard's events flow there with no
bespoke per-vendor code. (Langfuse: set its OTLP endpoint + auth on your OTel exporter.)

Event shape: {"ts", "kind": batch|realtime, "provider", "model", "cost", "decision", ...}.
Webhook/OTel run on a background daemon thread (drop-if-flooded) so high-volume real-time calls
are never slowed. Callbacks run inline — keep them fast.
"""
import json, os, threading, queue, datetime, urllib.request
from typing import Any, Callable
from . import config

_callbacks = []
_q = None
_worker = None
_lock = threading.Lock()
_cfg_cache = None
_cfg_mtime = 0.0        # config.json mtime the cache was built from — re-read when it changes (long-lived process)


def on_event(fn: "Callable[[dict], Any]") -> "Callable[[dict], Any]":
    """Register an in-process callback fn(event: dict). Usable as a decorator."""
    _callbacks.append(fn)
    return fn


def _cfg():
    global _cfg_cache, _cfg_mtime
    p = config.HOME / "config.json"
    try:
        mtime = p.stat().st_mtime if p.exists() else 0.0
    except OSError:
        mtime = 0.0
    if _cfg_cache is not None and mtime == _cfg_mtime:
        return _cfg_cache                       # cache invalidates when config.json changes (was cached for life)
    c = {}
    try:
        if p.exists():
            c = (json.loads(p.read_text()).get("emit") or {})
    except Exception:
        pass
    # Env WINS and can DISABLE, not just add: a var SET to an off value ('', '0', 'off', 'false', 'none') REMOVES
    # a config-enabled sink — there was previously no way to turn off a config webhook/otel from the environment.
    _off = ("", "0", "off", "false", "none")
    _wh = os.getenv("SPENDGUARD_WEBHOOK")
    if _wh is not None:
        if _wh.strip().lower() in _off:
            c.pop("webhook", None)
        else:
            c["webhook"] = _wh
    _ot = os.getenv("SPENDGUARD_OTEL")
    if _ot is not None:
        c["otel"] = _ot.strip().lower() not in _off
    _cfg_cache, _cfg_mtime = c, mtime
    return c


def _ensure_worker():
    global _q, _worker
    if _worker is not None:
        return
    with _lock:
        if _worker is not None:
            return
        _q = queue.Queue(maxsize=2000)
        _worker = threading.Thread(target=_drain, name="spendguard-emit", daemon=True)
        _worker.start()


def _drain():
    while True:
        event, cfg = _q.get()
        url = cfg.get("webhook")
        if url:
            try:
                req = urllib.request.Request(url, data=json.dumps(event).encode(),
                                             headers={"Content-Type": "application/json",
                                                      "User-Agent": "spendguard/0.1"}, method="POST")
                # `with` — the response socket was left to the GC. This runs on a long-lived background
                # thread draining a queue, so the leak accumulates for the life of the process rather
                # than being cleaned up at the end of a short call.
                with urllib.request.urlopen(req, context=config.ssl_context(), timeout=5) as resp:
                    resp.read()
            except Exception:
                pass
        if cfg.get("otel"):
            try:
                _otel(event)
            except Exception:
                pass


_otel_inst = None


def _otel_instruments():
    """Cache the OTel instruments once (creating per-event is wasteful)."""
    global _otel_inst
    if _otel_inst is None:
        from opentelemetry import metrics
        m = metrics.get_meter("spendguard")
        _otel_inst = (m.create_counter("spendguard.cost_usd", unit="USD"),
                      m.create_counter("gen_ai.client.token.usage", unit="token"))
    return _otel_inst


def _otel(event):
    # Emit using OpenTelemetry GenAI semantic conventions so it drops into ANY OTLP backend the user
    # already runs (Langfuse / Helicone / Phoenix / Honeycomb …). spendguard records to the GLOBAL
    # meter+tracer; the user's own OTel SDK + exporter route it — no bespoke per-vendor sink to break.
    from opentelemetry import trace
    attrs = {"gen_ai.system": str(event.get("provider", "")),
             "gen_ai.request.model": str(event.get("model", "")),
             "gen_ai.operation.name": str(event.get("kind", "")),
             "spendguard.decision": str(event.get("decision", ""))}
    cost_ctr, tok_ctr = _otel_instruments()
    cost_ctr.add(float(event.get("cost", 0) or 0), attrs)
    for kind_attr, key in (("input", "in_tok"), ("output", "out_tok")):
        v = event.get(key)
        if v:
            tok_ctr.add(int(v), {**attrs, "gen_ai.token.type": kind_attr})
    # a span too, for trace-based backends (Langfuse/Phoenix ingest spans)
    span = trace.get_tracer("spendguard").start_span("gen_ai." + str(event.get("kind", "call")), attributes=attrs)
    # try/finally: every set_attribute below coerces with int()/float() on a field that arrives from a
    # provider payload, so a non-numeric value raises BETWEEN start_span and end() and the span is never
    # ended. An unended span is not a missing span — it sits in the tracer as an open trace, and the
    # backend shows a call that started and never finished. The caller swallows the exception, so the
    # only visible symptom is a trace view that slowly fills with hanging spans.
    try:
        for k in ("in_tok", "out_tok", "cached_in_tok"):
            if event.get(k) is not None:
                span.set_attribute("gen_ai.usage." + k.replace("_tok", "_tokens"), int(event[k] or 0))
        if event.get("cost") is not None:
            span.set_attribute("spendguard.cost_usd", float(event["cost"] or 0))
    finally:
        span.end()


EVENT_V = 1   # event-envelope version — lets a consumer branch on shape as the event evolves


def envelope(event):
    """Normalize an event into the standard message envelope: { v, type, id, ts, conv_id, project, ...payload }.
    Tolerant — fills any missing envelope field, preserves everything else, never raises. `type` defaults from the
    event's `kind` (batch|realtime|…) so existing emitters need no change; `id` is a unique event id; `ts` is UTC
    ISO. `conv_id`/`project` are the SAME attribution the ledger records (budget._conv/_project) so a consumer
    (e.g. a live dashboard) can attach the event to the exact conversation/repo that spent the money."""
    import uuid
    e = dict(event or {})
    e.setdefault("v", EVENT_V)
    e.setdefault("type", e.get("kind") or "event")
    e.setdefault("id", uuid.uuid4().hex)
    e.setdefault("ts", datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"))
    try:
        from . import budget as _budget
        e.setdefault("conv_id", _budget._conv())
        e.setdefault("project", _budget._project())
    except Exception:
        pass
    return e


def emit(event):
    """Best-effort fan-out. NEVER raises (observability must not break enforcement)."""
    try:
        event = envelope(event)   # versioned, typed, id'd, timestamped envelope (tolerant — preserves payload)
        for fn in list(_callbacks):
            try:
                fn(event)
            except Exception:
                pass
        cfg = _cfg()
        if cfg.get("webhook") or cfg.get("otel"):
            _ensure_worker()
            try:
                _q.put_nowait((event, cfg))
            except queue.Full:
                pass
    except Exception:
        pass
