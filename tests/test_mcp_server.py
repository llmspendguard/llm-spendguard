"""`spendguard mcp` — the model-advisor over MCP (stdio JSON-RPC). Proves the protocol frame (initialize /
tools/list / tools/call / notifications / errors) and that the tools return real structured data from the local
corpus. Offline: no network, no LLM, no live pipe — handle() is exercised directly, then a full stdio round-trip
via StringIO. The advise tool must agree with advise.ranked (one computation, two surfaces)."""
import os
import sys
import io
import json
import tempfile

if not os.environ.get("SPENDGUARD_TEST_ISOLATED"):
    os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"
    os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="spendguard-mcp-")
    os.execv(sys.executable, [sys.executable] + sys.argv)

from spendguard import mcp_server, advise, calls

fails = []


from functools import partial
from _script_checks import record_script_check
ck = partial(record_script_check, fails, coerce_condition=True)


def rpc(method, params=None, rid=1, notification=False):
    req = {"jsonrpc": "2.0", "method": method}
    if not notification:
        req["id"] = rid
    if params is not None:
        req["params"] = params
    return mcp_server.handle(req)


# seed a small corpus so the advise tool has something to rank (one intent, cost + quality labels)
calls.insert("openai", "gpt-5.5", "batch", 1.0, in_tok=100_000, out_tok=200_000,
             ts="2026-06-10T10:00:00", intent="loinc-typing", quality="good", quality_conf=0.95)
calls.insert("anthropic", "claude-opus-4-8", "batch", 6.0, in_tok=50_000, out_tok=100_000,
             ts="2026-06-10T12:00:00", intent="loinc-typing", quality="good", quality_conf=0.9)

print("-- initialize: protocol handshake --")
r = rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {}})
ck("initialize returns a result with serverInfo.name = spendguard",
   r.get("result", {}).get("serverInfo", {}).get("name") == "spendguard")
ck("initialize echoes the client's protocolVersion", r["result"]["protocolVersion"] == "2025-06-18")
ck("initialize advertises the tools capability", "tools" in r["result"]["capabilities"])
ck("initialize carries an `instructions` string in the response frame (present, per MCP)",
   isinstance(r["result"].get("instructions"), str) and len(r["result"]["instructions"]) > 0)

print("-- notifications/initialized is a NOTIFICATION (no reply) --")
ck("notifications/initialized → None (no response frame)",
   mcp_server.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None)

print("-- tools/list: the advisor tools are advertised with schemas --")
tl = rpc("tools/list")["result"]["tools"]
names = {t["name"] for t in tl}
ck("both P1 tools listed", {"spendguard_advise", "spendguard_models"} <= names)
ck("the 5 spend/compaction tools are ALSO advertised (wired into _TOOLS, not just defined)",
   {"spendguard_spend_overview", "spendguard_overage_status", "spendguard_top_conversations",
    "spendguard_conversation_cost", "spendguard_compaction_candidates"} <= names)
ck("every tool carries a description + inputSchema",
   all(t.get("description") and isinstance(t.get("inputSchema"), dict) for t in tl))

print("-- spendguard_delegate: dry-run delegates to the engine and spends nothing --")
from spendguard import delegate_router as _delegate_router
_original_delegate_task = _delegate_router.delegate_task
_delegate_calls = []


def _stub_delegate_task(task, **kwargs):
    _delegate_calls.append((task, kwargs))
    return {"status": "estimate", "classification": {"kind": "agentic", "provider": "codex",
            "self_contained": True, "why": "bounded"},
            "estimate": {"plan": "codex", "real_api_usd": 0.0, "est_value_usd": 0.1,
                         "saved_overage_usd": 0.05}}


_delegate_router.delegate_task = _stub_delegate_task
try:
    delegated = rpc("tools/call", {"name": "spendguard_delegate", "arguments": {
        "task": "fix the parser", "files": ["src/parser.py"]}})["result"]["structuredContent"]
finally:
    _delegate_router.delegate_task = _original_delegate_task
ck("delegate MCP tool is advertised", "spendguard_delegate" in names)
ck("delegate dry-run returns the structured plan", delegated["status"] == "estimate")
ck("delegate dry-run defaults execute to false and forwards CLI-shaped arguments",
   _delegate_calls == [("fix the parser", {"files": ["src/parser.py"], "intent": None,
                                             "provider": "auto", "execute": False, "cwd": None})])

print("-- tools/call spendguard_advise: real ranking, agreeing with advise.ranked --")
ca = rpc("tools/call", {"name": "spendguard_advise", "arguments": {"intent": "loinc-typing"}})["result"]
ck("advise call is not an error", ca.get("isError") is False)
payload = ca["structuredContent"]
ck("the text content is the same JSON as structuredContent",
   json.loads(ca["content"][0]["text"])["pick"] == payload["pick"])
# gpt-5.5 $/good = 1.0/0.95 ; opus = 6.0/0.9 → gpt-5.5 is the cheaper-per-good pick
ck("pick is the cheapest-per-good model (openai:gpt-5.5)", payload["pick"] == "openai:gpt-5.5")
ck("ranked_by is $/good-result when quality is labeled", "good" in payload["ranked_by"])
ck("both models are in the ranking", {m["id"] for m in payload["models"]} == {"openai:gpt-5.5", "anthropic:claude-opus-4-8"})
ck("MCP answer matches the CLI computation exactly (advise.ranked)",
   payload["pick"] == advise.ranked(intent="loinc-typing")["pick"])

print("-- tools/call spendguard_models: the catalogue carries rates --")
cm = rpc("tools/call", {"name": "spendguard_models", "arguments": {}})["result"]["structuredContent"]
ck("catalogue has models with per-1M rates + provider",
   cm["count"] > 0 and all("in_usd_per_m" in m and m.get("provider") for m in cm["models"][:5]))
ck("a curated model (gpt-5.5) is in the catalogue", any(m["model"] == "gpt-5.5" for m in cm["models"]))
# The SHIPPED curated layer must surface on a FRESH install (this test's home is an empty temp dir: no
# litellm cache, no user prices.json), so the highest-volume Claude Code models are listed and priceable
# without waiting for `sync-prices`. Before the _catalogue() fix these were priced by price() yet invisible.
_by_id = {m["id"]: m for m in cm["models"]}
for _mid, _in, _out in (("anthropic:claude-opus-5", 5.0, 25.0),
                        ("anthropic:claude-fable-5", 10.0, 50.0),
                        ("anthropic:claude-fable-5-1", 10.0, 50.0)):
    ck(f"catalogue lists {_mid} at {_in}/{_out} from the shipped curated layer",
       _mid in _by_id and _by_id[_mid]["in_usd_per_m"] == _in and _by_id[_mid]["out_usd_per_m"] == _out)

print("-- errors: unknown tool + unknown method are reported without breaking the frame --")
ck("unknown tool name → JSON-RPC error -32602 (invalid params, not a silent empty result)",
   rpc("tools/call", {"name": "nope", "arguments": {}}).get("error", {}).get("code") == -32602)
ck("unknown method → JSON-RPC error -32601",
   rpc("bogus/method")["error"]["code"] == -32601)
ck("a tool that raises → isError, never a crash",
   rpc("tools/call", {"name": "spendguard_advise", "arguments": {"as_of": 12345}})["result"].get("isError") in (True, False))

print("-- stdio round-trip: newline-delimited JSON-RPC in → responses out --")
inp = io.StringIO("\n".join([
    json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}),
    json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}),        # notification → no output line
    json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                "params": {"name": "spendguard_advise", "arguments": {"intent": "loinc-typing"}}}),
]) + "\n")
outp = io.StringIO()
mcp_server.serve_stdio(inp, outp)
msgs = [json.loads(ln) for ln in outp.getvalue().splitlines() if ln.strip()]
responses = [m for m in msgs if "id" in m]
notifs = [m for m in msgs if m.get("method") == "notifications/tools/list_changed"]
ck("two requests produced two responses; the initialized notification produced none", len(responses) == 2)
ck("response ids match the request ids (1, 2)", [m["id"] for m in responses] == [1, 2])
ck("the second response carries the advise pick",
   responses[1]["result"]["structuredContent"]["pick"] == "openai:gpt-5.5")
ck("the server nudged the client to re-list tools once, after the first substantive request (tools/list_changed)",
   len(notifs) == 1)

print("-- (P2) advisor.recommend: estimate-first (zero spend) → cost + candidate set --")
os.environ["SPENDGUARD_ADVISOR_MODEL"] = "gpt-5-mini"      # a priced model so the estimate is deterministic
from spendguard import advisor, adapters as _ad
import spendguard as _sg
est = advisor.recommend_models(intent="loinc-typing", k=3, run=False)
ck("estimate returns a positive cost, the candidate set, and no spend",
   est.get("estimate_only") and est.get("cost", 0) > 0 and "openai:gpt-5.5" in est.get("candidates", []))

print("-- (P2) advisor.recommend: run path (stubbed reasoner) → structured, enriched with MEASURED $/good --")
_orig_call = _ad.call
_ad.call = lambda model, prompt, **kw: {                        # 'parsed' = the adapter's decoded structured object
    "parsed": {"quality_bar": {"level": "balanced", "why": "typing tolerates minor error"},
               "top": [{"id": "openai:gpt-5.5", "why": "cheapest per good result", "meets_bar": True}]},
    "text": "{}", "cost": 0.001, "error": None}
try:
    rec = advisor.recommend_models(intent="loinc-typing", k=3, run=True)
finally:
    _ad.call = _orig_call
ck("run returns the inferred quality_bar", (rec.get("quality_bar") or {}).get("level") == "balanced")
ck("top-K is enriched with the model's MEASURED per_good (agentic pick + real numbers)",
   rec.get("top") and rec["top"][0]["id"] == "openai:gpt-5.5" and rec["top"][0].get("per_good") is not None)

print("-- (P2) MCP spendguard_recommend SPENDS → estimates first + refuses over budget_usd (gate isolated) --")
_orig_req = _sg.require
_sg.require = lambda *a, **k: None                          # isolate the tool from gate-enforcement in this offline test
try:
    rr = rpc("tools/call", {"name": "spendguard_recommend",
                            "arguments": {"intent": "loinc-typing", "budget_usd": 1e-9}})["result"]["structuredContent"]
    ck("a tiny budget_usd REFUSES the spend (estimate over budget) before any LLM call", rr.get("refused") is True)
finally:
    _sg.require = _orig_req

print("-- (P3) bakeoff: estimate-first (zero spend) + clear errors when under-specified --")
from spendguard import bakeoff as _bk
be = _bk.bakeoff("code-review", candidates=["openai:gpt-5.5", "anthropic:claude-opus-4-8"],
                 prompts=["Review function A", "Review function B"], run=False)
ck("estimate returns a positive $ + per-candidate detail, no spend",
   be.get("estimate_only") and be.get("estimate_usd", 0) > 0
   and set(be.get("per_candidate", {})) == {"openai:gpt-5.5", "anthropic:claude-opus-4-8"})
ck("no candidates → a clear error (not a silent empty bakeoff)", bool(_bk.bakeoff("x", candidates=[]).get("error")))
ck("no sample tasks → a clear error naming what to pass",
   bool(_bk.bakeoff("intent-with-no-recorded-prompts", candidates=["openai:gpt-5.5"]).get("error")))

print("-- (P3) bakeoff RUN (stubbed models + judge): records + re-ranks; a failing candidate is SURFACED --")
_orig_bkcall = _ad.call


def _bake_stub(model, prompt, **kw):
    if model.startswith("flaky:"):
        return {"error": "rate_limited", "cost": None}                # this candidate fails every prompt
    if kw.get("schema"):                                              # the JUDGE call → a structured boolean verdict
        return {"json": {"good": True}, "text": '{"good": true}', "cost": 0.0001, "in_tok": 5, "out_tok": 3, "error": None}
    return {"text": "a candidate answer", "cost": 0.001, "in_tok": 10, "out_tok": 5, "error": None}   # candidate output


_ad.call = _bake_stub
try:
    rb = _bk.bakeoff("bakeoff-intent", candidates=["openai:gpt-5.5", "flaky:model-x"],
                     prompts=["task 1", "task 2"], run=True, budget_usd=1.0)
finally:
    _ad.call = _orig_bkcall
ck("a good candidate ran BOTH prompts and was judged good",
   rb["per_candidate"]["openai:gpt-5.5"]["runs"] == 2 and rb["per_candidate"]["openai:gpt-5.5"]["good"] == 2)
ck("a candidate that fails every prompt has its drops COUNTED + surfaced (not a silent zero-run)",
   rb["per_candidate"]["flaky:model-x"]["failed"] == 2 and bool(rb["per_candidate"]["flaky:model-x"]["last_error"]))
ck("the bakeoff'd candidate now appears in the re-ranking (recorded to the corpus advise reads)",
   any(m["id"] == "openai:gpt-5.5" for m in rb.get("ranking", [])))

print("-- (P3) MCP spendguard_bakeoff: NO budget_usd → estimate only, never auto-spends --")
_sg.require = lambda *a, **k: None
try:
    rt = rpc("tools/call", {"name": "spendguard_bakeoff",
                            "arguments": {"intent": "code-review", "candidates": ["openai:gpt-5.5"],
                                          "prompts": ["review this"]}})["result"]["structuredContent"]
    ck("without budget_usd the tool returns an ESTIMATE and does not run", rt.get("estimate_only") is True)
finally:
    _sg.require = _orig_req

print("-- (spend/compaction) the read-only Claude Code tools: no crash on an EMPTY ledger + two axes stay SEPARATE --")
# Hermetic: stub the transcript-reading helpers so this exercises the PROTOCOL + payload SHAPE, not the real
# ~/.claude transcripts (which would be non-hermetic and slow). The DB is the isolated empty test home.
from spendguard import claudecode as _cc, compaction as _cp
_cc._sidebar_titles = lambda: {}
_cc._overage_events = lambda *a, **k: {"weekly_resets": [], "weekly_hits": [], "blocked": [], "overage_direct": [], "five_hour_hits": []}
_cc._overage_windows = lambda *a, **k: ([], None)
_cc.attribute_overage = lambda *a, **k: {}
_cc.compaction_candidates = lambda *a, **k: ([], (1.0, 0), {"examined": 0, "flagged": 0})
_cp.compact_snippet = lambda *a, **k: "/compact <keep the goal + open files>"

ov = rpc("tools/call", {"name": "spendguard_spend_overview", "arguments": {}})["result"]
ck("spend_overview is not an error on an empty ledger", ov.get("isError") is False)
ovp = ov["structuredContent"]
ck("spend_overview keeps REAL $ and est-value as SEPARATE axes (the two-axis contract)",
   "real_usd" in ovp and "est_value_usd" in ovp)
ck("spend_overview NEVER exposes a single summed grand-total that mixes the two axes",
   not any("est" in k and "real" in k for k in ovp) and "grand_total" not in ovp)

st = rpc("tools/call", {"name": "spendguard_overage_status", "arguments": {}})["result"]["structuredContent"]
ck("overage_status preserves stale status as explicit UNKNOWN",
   st.get("on_overage_now") is None and st.get("stale") is True and "unknown" in st.get("meaning", ""))

tc = rpc("tools/call", {"name": "spendguard_top_conversations", "arguments": {"by": "est", "limit": 3}})["result"]
ck("top_conversations returns a conversations list (empty ok on an empty ledger)",
   tc.get("isError") is False and isinstance(tc["structuredContent"].get("conversations"), list))

cc = rpc("tools/call", {"name": "spendguard_conversation_cost", "arguments": {"conversation_id": "does-not-exist"}})["result"]["structuredContent"]
ck("conversation_cost keeps the two axes separate (est-value vs overage upper bound)",
   "est_value_usd_plan_covered" in cc and "observed_overage_usd_upper_bound" in cc)

cand = rpc("tools/call", {"name": "spendguard_compaction_candidates", "arguments": {"limit": 3}})["result"]
ck("compaction_candidates returns a candidates list + a ready-to-paste /compact command",
   cand.get("isError") is False and isinstance(cand["structuredContent"].get("candidates"), list)
   and str(cand["structuredContent"].get("effective_compact_command", "")).startswith("/compact"))

print("\n-- cmd --http is a WARM-DAEMON SINGLETON (a second start exits, never double-binds) --")
# The HTTP transport (serve_http) routes POST /mcp through the SAME handle() as stdio, so every session CONNECTS to
# one warm daemon instead of spawning a `spendguard mcp` stdio copy. --http must be a singleton: a second start with
# the port's lock held exits without serving (no http.server bind crash), so a supervisor can (re)start it idempotently.
import contextlib
from spendguard import config as _cfg
_served = {"n": 0}
_orig_serve, _orig_lock = mcp_server.serve_http, _cfg.single_instance_lock
try:
    mcp_server.serve_http = lambda host="127.0.0.1", port=4913: _served.__setitem__("n", _served["n"] + 1)
    _cfg.single_instance_lock = lambda name: contextlib.nullcontext(False)    # lock held by the incumbent
    mcp_server.cmd(["--http", "--port", "4913"])
    ck("a second --http start (lock held) does NOT serve a second daemon", _served["n"] == 0)
    _cfg.single_instance_lock = lambda name: contextlib.nullcontext(True)      # no incumbent
    mcp_server.cmd(["--http", "--port", "4913"])
    ck("--http serves when the lock is free", _served["n"] == 1)
finally:
    mcp_server.serve_http, _cfg.single_instance_lock = _orig_serve, _orig_lock

print("\n-- HTTP transport: a NOTIFICATION POST gets 202 (no body), never a 200+empty-JSON that kills strict rmcp --")
# ANTI-REGRESSION for the warm-daemon churn (first seen 2026-10-22 ~23:44:27 in the codex app-server logs):
# codex's rmcp `StreamableHttpClientWorker` POSTs `notifications/initialized` during the handshake, then reads the
# response. The old serve_http answered a notification with `200 + Content-Type: application/json + empty body`;
# rmcp tried to serde_json-parse the zero-byte body and died with "Deserialize error: EOF while parsing a value at
# line 1 column 0, when send initialized notification", quitting the transport worker and reconnecting in a loop.
# Per the MCP Streamable HTTP transport a notification-only POST MUST get `202 Accepted` with NO body. We replay the
# EXACT offending payload through the real HTTP handler over an in-process socket pair and assert the fixed shape —
# the unit of the defect is the HTTP response, so the guard must exercise the handler, not just call handle().
import http.client as _http_client
import socket as _socket

os.environ["SPENDGUARD_MCP_AUTO_RESPAWN"] = "0"   # no mid-request self-heal _exit during the test
_JSON_RPC_VERSION = "2.0"
_INITIALIZED_NOTIFICATION_METHOD = "notifications/initialized"
_HTTP_OK_STATUS = 200
_HTTP_ACCEPTED_STATUS = 202
_INITIALIZED_NOTIFICATION_REPETITIONS = 2
_IN_PROCESS_PEER_ADDRESS = ("in-process", 0)
_HTTP_CONTENT_TYPE_HEADER = "Content-Type"
_HTTP_CONTENT_LENGTH_HEADER = "Content-Length"

def _post_mcp_in_process(handler_class, payload):
    data = json.dumps(payload).encode("utf-8")
    request_bytes = (
        b"POST /mcp HTTP/1.1\r\n"
        + b"Host: in-process\r\n"
        + f"{_HTTP_CONTENT_TYPE_HEADER}: application/json\r\n".encode("ascii")
        + b"Accept: application/json, text/event-stream\r\n"
        + f"{_HTTP_CONTENT_LENGTH_HEADER}: {len(data)}\r\n".encode("ascii")
        + b"Connection: close\r\n\r\n"
        + data
    )
    client_socket, server_socket = _socket.socketpair()
    try:
        client_socket.sendall(request_bytes)
        client_socket.shutdown(_socket.SHUT_WR)
        handler_class(server_socket, _IN_PROCESS_PEER_ADDRESS, object())
        response = _http_client.HTTPResponse(client_socket)
        response.begin()
        return response.status, response.getheader(_HTTP_CONTENT_TYPE_HEADER), response.read()
    finally:
        server_socket.close()
        client_socket.close()

_http_handler_class = mcp_server.build_mcp_http_handler()
# a REQUEST (has id) still returns 200 + a parseable JSON-RPC result body — unchanged
st, ct, body = _post_mcp_in_process(
    _http_handler_class,
    {"jsonrpc": _JSON_RPC_VERSION, "id": 1, "method": "initialize",
     "params": {"protocolVersion": "2025-06-18", "capabilities": {}}},
)
ck("HTTP initialize request → 200 with a JSON body that parses to a result frame",
   st == _HTTP_OK_STATUS
   and json.loads(body).get("result", {}).get("serverInfo", {}).get("name") == "spendguard")

# Replay the EXACT payload that killed the rmcp worker TWICE. Idempotent re-initialization does not turn the
# second envelope into a request: every response must independently remain 202 with literally zero body bytes.
_initialized_notification = {"jsonrpc": _JSON_RPC_VERSION, "method": _INITIALIZED_NOTIFICATION_METHOD}
for _attempt_number in range(_INITIALIZED_NOTIFICATION_REPETITIONS):
    st, ct, body = _post_mcp_in_process(_http_handler_class, _initialized_notification)
    ck(f"HTTP notifications/initialized attempt {_attempt_number + 1} → 202 Accepted",
       st == _HTTP_ACCEPTED_STATUS)
    ck(f"HTTP notifications/initialized attempt {_attempt_number + 1} → EMPTY body", body == b"")
    ck("HTTP notification is NOT answered with 200+application/json+empty (the rmcp EOF-at-col-0 crash shape)",
       not (st == _HTTP_OK_STATUS and (ct or "").startswith("application/json") and body == b""))

# an UNKNOWN notification is also just acknowledged (202), never a reply frame
st, _ct, body = _post_mcp_in_process(
    _http_handler_class, {"jsonrpc": _JSON_RPC_VERSION, "method": "notifications/cancelled"}
)
ck("HTTP unknown notification → 202 Accepted, empty body", st == _HTTP_ACCEPTED_STATUS and body == b"")

# Simulate the reported alternate/idempotent re-init dispatcher branch returning an acknowledgement frame. The HTTP
# response seam must derive notification semantics from the inbound envelope, never from what a branch returned.
_original_http_dispatch = mcp_server.handle
_repeat_dispatch_count = {"value": 0}


def _dispatch_repeated_initialized_with_ack(request):
    if "id" not in request and request.get("method") == _INITIALIZED_NOTIFICATION_METHOD:
        _repeat_dispatch_count["value"] += 1
        if _repeat_dispatch_count["value"] > 1:
            return {"jsonrpc": _JSON_RPC_VERSION, "result": {}}
    return _original_http_dispatch(request)


mcp_server.handle = _dispatch_repeated_initialized_with_ack
_http_handler_class = mcp_server.build_mcp_http_handler()
try:
    _post_mcp_in_process(_http_handler_class, _initialized_notification)
    st, _ct, body = _post_mcp_in_process(_http_handler_class, _initialized_notification)
    ck("HTTP response seam keeps a repeated initialized notification at 202+empty even if dispatch returns an ack",
       st == _HTTP_ACCEPTED_STATUS and body == b"")
finally:
    mcp_server.handle = _original_http_dispatch

print(f"\n{'[FAIL]' if fails else 'OK'} test_mcp_server: {len(fails)} failure(s)")
sys.exit(1 if fails else 0)
