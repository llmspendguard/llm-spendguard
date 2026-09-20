"""GUARD — the MCP server exposes its served version and SELF-HEALS to the latest deployed code. This is the
server-side half of "always serve the latest with the best": a long-running stdio server, on its next request
after a deploy, hands off to a fresh process on the green commit — and NEVER mid-request (no dropped call) and
NEVER in a loop (only when a fresh process would actually reach green).

Pins: (1) spendguard_version is a listed tool and returns release.release_status(); (2) initialize's serverInfo
carries the served short sha; (3) serve_stdio drains-then-exits after a response when should_respawn() is True,
processing NO further request; (4) with auto-respawn OFF it keeps serving; (5) the env toggle parses.

Hermetic: release is stubbed (no git, no pointer); serve_stdio is driven with StringIO; no network, no ledger."""
import io
import json
import os
import sys
import tempfile

os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="spendguard-mcpver-")
os.environ.setdefault("SPENDGUARD_TEST_ISOLATED", "1")
os.environ.setdefault("SPENDGUARD_NO_AUTOINSTALL", "1")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from spendguard import mcp_server, release   # noqa: E402

fails = []


def ck(name, cond):
    print(("  [OK] " if cond else "  [FAIL] ") + name)
    if not cond:
        fails.append(name)


def _lines(sio):
    return [ln for ln in sio.getvalue().splitlines() if ln.strip()]


PING = '{"jsonrpc":"2.0","id":%d,"method":"ping"}'

print("-- spendguard_version is a listed tool and returns release_status --")
ck("spendguard_version is in the tool registry", "spendguard_version" in mcp_server._TOOLS)
listed = mcp_server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
names = [t["name"] for t in listed["result"]["tools"]]
ck("spendguard_version appears in tools/list", "spendguard_version" in names)

release.release_status = lambda: {"served": {"short": "aaaaaaa"}, "green": {"short": "bbbbbbb"}, "stale": True,
                                  "note": "STALE: served aaaaaaa, green bbbbbbb"}
res = mcp_server.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                         "params": {"name": "spendguard_version", "arguments": {}}})
payload = json.loads(res["result"]["content"][0]["text"])
ck("the version tool returns release_status (served/green/stale)",
   payload.get("stale") is True and payload["served"]["short"] == "aaaaaaa" and payload["green"]["short"] == "bbbbbbb")

print("\n-- initialize's serverInfo carries the served short sha --")
release.served_sha = lambda: {"short": "abc1234", "sha": "abc1234" + "0" * 33}
init = mcp_server.handle({"jsonrpc": "2.0", "id": 3, "method": "initialize", "params": {"protocolVersion": "2024-11-05"}})
ver = init["result"]["serverInfo"]["version"]
ck("serverInfo.version includes the served short sha (e.g. 0.10.0+abc1234)", ver.endswith("+abc1234"))
ck("initialize advertises tools.listChanged (so the client tracks re-list nudges)",
   init["result"]["capabilities"]["tools"].get("listChanged") is True)

print("\n-- serve_stdio DRAINS then exits after a response when should_respawn() is True (no dropped/looped request) --")
os.environ["SPENDGUARD_MCP_AUTO_RESPAWN"] = "1"
release.should_respawn = lambda: True
inp = io.StringIO("\n".join([PING % 10, PING % 11, PING % 12]) + "\n")
outp = io.StringIO()
mcp_server.serve_stdio(inp, outp)
out = _lines(outp)
ck("exactly ONE response was written before the handoff (the served request was NOT dropped)", len(out) == 1)
ck("it was the FIRST request (id=10); the 2nd/3rd were left for the fresh process", json.loads(out[0])["id"] == 10)

print("\n-- with auto-respawn OFF, the server keeps serving; and it nudges the client to re-list tools once --")
os.environ["SPENDGUARD_MCP_AUTO_RESPAWN"] = "0"
release.should_respawn = lambda: True            # would respawn, but the toggle is off
inp2 = io.StringIO("\n".join([PING % 20, PING % 21, PING % 22]) + "\n")
outp2 = io.StringIO()
mcp_server.serve_stdio(inp2, outp2)
msgs = [json.loads(ln) for ln in _lines(outp2)]
responses = [m for m in msgs if "id" in m]
notifs = [m for m in msgs if m.get("method") == "notifications/tools/list_changed"]
ck("all three requests are served when auto-respawn is disabled", len(responses) == 3)
ck("the server emits tools/list_changed exactly ONCE (so a newly-added tool appears without a manual reconnect)",
   len(notifs) == 1)

print("\n-- the env toggle parses truthy/falsey --")
for val, want in [("1", True), ("true", True), ("on", True), ("0", False), ("false", False), ("off", False)]:
    os.environ["SPENDGUARD_MCP_AUTO_RESPAWN"] = val
    ck(f"SPENDGUARD_MCP_AUTO_RESPAWN={val!r} → {want}", mcp_server._auto_respawn_enabled() is want)

print(f"\n{'[FAIL]' if fails else 'OK'} test_mcp_version_and_respawn: {len(fails)} failure(s)")
sys.exit(1 if fails else 0)
