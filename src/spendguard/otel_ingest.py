"""OTel GenAI span ingest — adopt the OpenTelemetry GenAI span semantic conventions as spendguard's LEDGER
INTERCHANGE FORMAT, so an LLM call already traced by OpenLLMetry / Traceloop / any OTel GenAI instrumentation
lands in the ledger as a spend_events row without re-instrumenting anything. This is how spendguard captures spend
from a runtime it does NOT gate (a different language, a service already wired for OTel): the trace is the receipt.

INPUT: an OTLP/JSON export (resourceSpans → scopeSpans → spans), a bare list of spans, {"spans": [...]}, or one
span — each span's attributes either the OTLP [{key, value:{stringValue|intValue|doubleValue}}] list or a flat
{key: value} dict. Only GenAI spans (a gen_ai.provider.name / gen_ai.system, or a gen_ai.operation.name) are taken.

COST: the GenAI spec carries NO cost attribute, so unless the exporter added one (gen_ai.usage.cost /
llm.usage.total_cost) the cost is RECONSTRUCTED from the tokens via pricing.realtime_cost — basis=reconstructed
(derived after the fact from a trace, never presented as a live gate charge or a provider bill). An unknown model
records UNPRICED (tokens kept, $ unknown) rather than a silent $0. IDEMPOTENT on the span id, so re-ingesting a
trace file never double-counts. No LLM, no network. `spendguard otel-ingest <file.json> [--project P] [--dry-run]`.
"""
import datetime
import json
import sys

from . import budget

# provider / model / token / operation attribute keys, current OTel GenAI names first, then the widely-deployed
# older ones (OpenLLMetry/Traceloop shipped gen_ai.system + prompt/completion_tokens for a long time). Reading a
# known-shape attribute by a fixed key is PARSING, not a judgement — a fixed list of aliases is correct here.
_PROVIDER_KEYS = ("gen_ai.provider.name", "gen_ai.system")
_MODEL_KEYS = ("gen_ai.response.model", "gen_ai.request.model", "gen_ai.model")
_IN_TOK_KEYS = ("gen_ai.usage.input_tokens", "gen_ai.usage.prompt_tokens", "llm.usage.prompt_tokens")
_OUT_TOK_KEYS = ("gen_ai.usage.output_tokens", "gen_ai.usage.completion_tokens", "llm.usage.completion_tokens")
_CACHE_TOK_KEYS = ("gen_ai.usage.cache_read.input_tokens", "gen_ai.usage.cache_read_input_tokens")
_COST_KEYS = ("gen_ai.usage.cost", "llm.usage.total_cost", "gen_ai.usage.total_cost")
_OP_KEYS = ("gen_ai.operation.name", "llm.request.type")


def _otlp_value(v):
    """One OTLP attribute value union → a Python scalar. intValue is a STRING in OTLP/JSON (proto int64). Parsing a
    fixed wire shape, never a decision."""
    if not isinstance(v, dict):
        return v
    if "stringValue" in v:
        return v["stringValue"]
    if "intValue" in v:
        try:
            return int(v["intValue"])
        except (TypeError, ValueError):
            return None
    if "doubleValue" in v:
        try:
            return float(v["doubleValue"])
        except (TypeError, ValueError):
            return None
    if "boolValue" in v:
        return bool(v["boolValue"])
    return None


def _attr_map(attributes):
    """A span's attributes → flat {key: value}, whether they arrive as the OTLP list [{key, value:{…}}] or an
    already-flat dict."""
    if isinstance(attributes, dict):
        return dict(attributes)
    out = {}
    for a in attributes or []:
        if isinstance(a, dict) and "key" in a:
            out[a["key"]] = _otlp_value(a.get("value"))
    return out


def _iter_spans(doc):
    """Yield raw span dicts from any of the accepted shapes: OTLP (resourceSpans → scopeSpans → spans), a bare list,
    {'spans': [...]}, or a single span."""
    if isinstance(doc, list):
        for s in doc:
            yield from _iter_spans(s)
        return
    if not isinstance(doc, dict):
        return
    if "resourceSpans" in doc:
        for rs in doc.get("resourceSpans") or []:
            for ss in rs.get("scopeSpans") or rs.get("instrumentationLibrarySpans") or []:
                for sp in ss.get("spans") or []:
                    yield sp
        return
    if "spans" in doc and isinstance(doc["spans"], list):
        for sp in doc["spans"]:
            yield sp
        return
    yield doc                                            # a single span


def _first(attrs, keys):
    for k in keys:
        if attrs.get(k) not in (None, ""):
            return attrs[k]
    return None


def _span_time(span):
    """A span's start time → an ISO occurred_at, from startTimeUnixNano (OTLP) or an ISO/startTime field. None when
    absent (the writer then stamps 'now')."""
    nano = span.get("startTimeUnixNano") or span.get("start_time_unix_nano")
    if nano is not None:
        try:
            return datetime.datetime.fromtimestamp(int(nano) / 1e9, tz=datetime.timezone.utc).isoformat(timespec="seconds")
        except (TypeError, ValueError, OSError):
            pass
    t = span.get("startTime") or span.get("start_time")
    return t if isinstance(t, str) else None


def span_to_charge(span):
    """Map ONE span to a charge dict, or None if it is not a GenAI span. Keys: provider, model, in_tok, out_tok,
    cache_tok, cost (None → compute/unpriced downstream), operation, span_id, occurred_at."""
    attrs = _attr_map(span.get("attributes"))
    provider = _first(attrs, _PROVIDER_KEYS)
    operation = _first(attrs, _OP_KEYS)
    if provider is None and operation is None:
        return None                                      # not a GenAI span
    cost = _first(attrs, _COST_KEYS)
    return {
        "provider": (provider or "otel").split(".")[-1],  # 'gcp.vertex_ai' → 'vertex_ai'; a bare provider is unchanged
        "model": _first(attrs, _MODEL_KEYS) or "?",
        "in_tok": int(_first(attrs, _IN_TOK_KEYS) or 0),
        "out_tok": int(_first(attrs, _OUT_TOK_KEYS) or 0),
        "cache_tok": int(_first(attrs, _CACHE_TOK_KEYS) or 0),
        "cost": (float(cost) if cost is not None else None),
        "operation": operation or "chat",
        "span_id": span.get("spanId") or span.get("span_id") or "",
        "occurred_at": _span_time(span),
    }


def _cost_for(ch):
    """(cost, basis) for a charge: the span's own cost (reconstructed from the trace) if present, else priced from
    the tokens; an unpriceable model → (None, unpriced) so tokens are kept and the $ is honestly UNKNOWN, not $0."""
    if ch["cost"] is not None:
        return ch["cost"], budget.BASIS_RECONSTRUCTED
    try:
        from . import pricing
        return pricing.realtime_cost(ch["model"], ch["in_tok"], ch["out_tok"], ch["cache_tok"]), budget.BASIS_RECONSTRUCTED
    except Exception:
        return None, budget.BASIS_UNPRICED


def ingest(doc, project=None, dry_run=False):
    """Record every GenAI span in `doc` as a spend_events row (basis reconstructed / unpriced). IDEMPOTENT on the
    span id — re-ingesting the same trace never double-counts. dry_run counts + prices without writing. Returns a
    summary: {ingested, skipped, unpriced, cost, spans}."""
    ingested = skipped = unpriced = 0
    total = 0.0
    for span in _iter_spans(doc):
        ch = span_to_charge(span)
        if ch is None:
            skipped += 1
            continue
        cost, basis = _cost_for(ch)
        if basis == budget.BASIS_UNPRICED:
            unpriced += 1
        else:
            total += float(cost or 0)
        if not dry_run:
            # STABLE dedup key on the span id (or its content when a span carries no id) → re-ingesting the same
            # trace file is idempotent, never a double-count.
            stable = "otel:" + (ch["span_id"] or "%s:%s:%s:%s" % (ch["occurred_at"], ch["model"], ch["in_tok"], ch["out_tok"]))
            budget._record_spend_event(
                ch["provider"], ch["model"], "realtime", float(cost or 0),
                basis=basis, intent=("otel:" + ch["operation"])[:120], project=project or "",
                occurred_at=ch["occurred_at"], in_tok=ch["in_tok"], out_tok=ch["out_tok"],
                source="otel", dedup_key=stable)
        ingested += 1
    return {"ingested": ingested, "skipped": skipped, "unpriced": unpriced,
            "cost": round(total, 6), "spans": ingested + skipped}


def main(argv=None):
    argv = list(argv if argv is not None else sys.argv[1:])
    paths = [a for a in argv if not a.startswith("--")]
    dry = "--dry-run" in argv
    project = (argv[argv.index("--project") + 1]
               if "--project" in argv and argv.index("--project") + 1 < len(argv) else None)
    if not paths:
        sys.stderr.write("usage: spendguard otel-ingest <trace.json> [--project P] [--dry-run]\n")
        return 2
    with open(paths[0]) as f:
        doc = json.load(f)
    s = ingest(doc, project=project, dry_run=dry)
    verb = "would ingest" if dry else "ingested"
    sys.stdout.write(
        "otel-ingest: %s %d GenAI span(s) → $%.6f reconstructed%s (%d non-GenAI skipped)%s\n"
        % (verb, s["ingested"], s["cost"],
           (", %d UNPRICED (tokens kept, $ unknown)" % s["unpriced"]) if s["unpriced"] else "",
           s["skipped"], "  [dry-run: nothing written]" if dry else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
