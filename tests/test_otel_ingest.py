"""OTel GenAI span ingest → spend_events. Imports an LLM call already traced by OpenLLMetry/Traceloop/any OTel
GenAI instrumentation, from a runtime spendguard does NOT gate, without re-instrumenting. Guards: the OTLP/JSON
walk + value-union parse; BOTH attribute generations (current gen_ai.provider.name/input_tokens AND the older
gen_ai.system/prompt_tokens); cost = the span's own if present else priced from tokens else UNPRICED (never a
silent $0); idempotency on span id (re-ingest never double-counts); non-GenAI spans skipped. Offline, isolated, $0."""
import os, sys, tempfile

if not os.environ.get("SPENDGUARD_TEST_ISOLATED"):
    os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"
    os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="spendguard-otel-")
    os.execv(sys.executable, [sys.executable] + sys.argv)

from spendguard import otel_ingest as O
from spendguard import budget

fails = []
def ck(name, cond):
    print(("  [OK] " if cond else "  [FAIL] ") + name)
    if not cond:
        fails.append(name)

def attr(k, v, t="stringValue"):
    return {"key": k, "value": {t: v}}

# 2026-06-15T12:00:00Z in nanoseconds (fixed, so occurred_at is deterministic)
import datetime as _dt
NANO = str(int(_dt.datetime(2026, 6, 15, 12, 0, 0, tzinfo=_dt.timezone.utc).timestamp() * 1_000_000_000))

# an OTLP/JSON doc: a CURRENT-convention OpenAI chat span (priced from tokens), an OLDER-convention Anthropic span
# carrying its own cost attr, an unknown-model span (→ UNPRICED), and a non-GenAI span (→ skipped).
DOC = {"resourceSpans": [{"scopeSpans": [{"spans": [
    {"spanId": "aaaa", "startTimeUnixNano": NANO, "name": "chat gpt-5.5", "attributes": [
        attr("gen_ai.provider.name", "openai"),
        attr("gen_ai.request.model", "gpt-5.5"),
        attr("gen_ai.operation.name", "chat"),
        attr("gen_ai.usage.input_tokens", "1000", "intValue"),
        attr("gen_ai.usage.output_tokens", "500", "intValue")]},
    {"spanId": "bbbb", "startTimeUnixNano": NANO, "name": "chat claude", "attributes": [
        attr("gen_ai.system", "anthropic"),                       # older provider key
        attr("gen_ai.response.model", "claude-opus-4-8"),
        attr("gen_ai.operation.name", "chat"),
        attr("gen_ai.usage.prompt_tokens", "2000", "intValue"),   # older token keys
        attr("gen_ai.usage.completion_tokens", "800", "intValue"),
        attr("gen_ai.usage.cost", 0.123, "doubleValue")]},        # exporter-supplied cost
    {"spanId": "cccc", "startTimeUnixNano": NANO, "name": "chat mystery", "attributes": [
        attr("gen_ai.provider.name", "whoknows"),
        attr("gen_ai.request.model", "made-up-model-xyz"),
        attr("gen_ai.operation.name", "chat"),
        attr("gen_ai.usage.input_tokens", "10", "intValue"),
        attr("gen_ai.usage.output_tokens", "5", "intValue")]},
    {"spanId": "dddd", "name": "GET /health", "attributes": [attr("http.method", "GET")]},  # NOT a GenAI span
]}]}]}

print("-- (1) span_to_charge maps both attribute generations + skips non-GenAI --")
spans = list(O._iter_spans(DOC))
ck("OTLP walk yields all 4 spans", len(spans) == 4)
c0 = O.span_to_charge(spans[0])
ck("current names: provider/model/tokens", c0["provider"] == "openai" and c0["model"] == "gpt-5.5"
   and c0["in_tok"] == 1000 and c0["out_tok"] == 500)
c1 = O.span_to_charge(spans[1])
ck("older names (gen_ai.system + prompt/completion_tokens) parse too",
   c1["provider"] == "anthropic" and c1["in_tok"] == 2000 and c1["out_tok"] == 800 and c1["cost"] == 0.123)
ck("a non-GenAI span → None (skipped)", O.span_to_charge(spans[3]) is None)

print("-- (2) cost: priced from tokens, exporter cost honoured, unknown model → UNPRICED --")
cost0, basis0 = O._cost_for(c0)
ck("openai gpt-5.5 1000/500 priced from tokens ($0.02, reconstructed)",
   abs(cost0 - 0.02) < 1e-9 and basis0 == budget.BASIS_RECONSTRUCTED)
cost1, _ = O._cost_for(c1)
ck("the exporter's own cost attr is used when present ($0.123)", abs(cost1 - 0.123) < 1e-9)
_, basis2 = O._cost_for(O.span_to_charge(spans[2]))
ck("an unpriceable model → UNPRICED (tokens kept, $ unknown — never a silent $0)", basis2 == budget.BASIS_UNPRICED)

print("-- (3) ingest writes spend_events with the right facts, and is IDEMPOTENT on span id --")
s = O.ingest(DOC, project="traced-svc")
ck("summary: 3 GenAI ingested, 1 skipped, 1 unpriced", s["ingested"] == 3 and s["skipped"] == 1 and s["unpriced"] == 1)
ck("summary cost = 0.02 + 0.123 (unpriced adds nothing)", abs(s["cost"] - 0.143) < 1e-9)
rows = [r for r in budget._ledger().query() if r.get("source") == "otel"]
ck("3 otel rows written", len(rows) == 3)
_by_model = {r["model"]: r for r in rows}
ck("openai row: realtime, tokens, occurred_at from the span, intent from the operation",
   _by_model["gpt-5.5"]["in_tok"] == 1000 and _by_model["gpt-5.5"]["out_tok"] == 500
   and _by_model["gpt-5.5"]["occurred_at"].startswith("2026-06-15") and _by_model["gpt-5.5"]["intent"] == "otel:chat")
O.ingest(DOC, project="traced-svc")                      # re-ingest the SAME trace
rows2 = [r for r in budget._ledger().query() if r.get("source") == "otel"]
ck("re-ingesting the same trace does NOT double-count (idempotent on span id)", len(rows2) == 3)

print("-- (4) accepts the other shapes: a bare list, {spans}, a single span, flat-dict attributes --")
one = {"spanId": "z1", "attributes": {"gen_ai.system": "openai", "gen_ai.request.model": "gpt-5.5",
                                      "gen_ai.usage.input_tokens": 100, "gen_ai.usage.output_tokens": 50}}
ck("flat-dict attributes parse", O.span_to_charge(one)["in_tok"] == 100)
ck("a bare list of spans is walked", len(list(O._iter_spans([one, one]))) == 2)
ck("{'spans': [...]} is walked", len(list(O._iter_spans({"spans": [one]}))) == 1)
ck("dry-run counts but writes nothing new",
   O.ingest({"spans": [one]}, dry_run=True)["ingested"] == 1
   and len([r for r in budget._ledger().query() if r.get("source") == "otel"]) == 3)

print(("\n[OK] " if not fails else "\n[FAIL] ") + f"otel_ingest: {len(fails)} failure(s)")
sys.exit(1 if fails else 0)
