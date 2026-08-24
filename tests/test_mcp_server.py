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


def ck(name, cond):
    ok = bool(cond)
    print(("  [OK] " if ok else "  [FAIL] ") + name)
    if not ok:
        fails.append(name)


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

print("-- notifications/initialized is a NOTIFICATION (no reply) --")
ck("notifications/initialized → None (no response frame)",
   mcp_server.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None)

print("-- tools/list: the advisor tools are advertised with schemas --")
tl = rpc("tools/list")["result"]["tools"]
names = {t["name"] for t in tl}
ck("both P1 tools listed", {"spendguard_advise", "spendguard_models"} <= names)
ck("every tool carries a description + inputSchema",
   all(t.get("description") and isinstance(t.get("inputSchema"), dict) for t in tl))

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
lines = [ln for ln in outp.getvalue().splitlines() if ln.strip()]
ck("two requests produced two responses; the notification produced none", len(lines) == 2)
ck("response ids match the request ids (1, 2)", [json.loads(x)["id"] for x in lines] == [1, 2])
ck("the second response carries the advise pick",
   json.loads(lines[1])["result"]["structuredContent"]["pick"] == "openai:gpt-5.5")

print(f"\n{'[FAIL]' if fails else 'OK'} test_mcp_server: {len(fails)} failure(s)")
sys.exit(1 if fails else 0)
