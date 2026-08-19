"""Grounding probe: is `codex mcp-server` a viable WARM lane? Spawn it once, do the MCP handshake, list its tools,
and (if a run/prompt-style tool exists) call it TWICE to measure cold vs warm latency. Reveals the interface the
spendguard codex-daemon lane would speak. Throwaway diagnostic — prints findings, changes nothing.
"""
import json
import subprocess
import sys
import time
import select


def _send(p, obj):
    p.stdin.write(json.dumps(obj) + "\n")
    p.stdin.flush()


def _read_until(p, want_id, timeout=60):
    """Read line-delimited JSON-RPC until a response with id==want_id (skip notifications/other ids)."""
    end = time.time() + timeout
    while time.time() < end:
        r, _, _ = select.select([p.stdout], [], [], end - time.time())
        if not r:
            continue
        line = p.stdout.readline()
        if not line:
            return None
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except Exception:
            continue
        if msg.get("id") == want_id:
            return msg
    return None


def main():
    exe = subprocess.run(["bash", "-lc", "command -v codex"], capture_output=True, text=True).stdout.strip()
    print("codex:", exe or "NOT FOUND")
    p = subprocess.Popen([exe, "mcp-server"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                         stderr=subprocess.DEVNULL, text=True, bufsize=1)
    try:
        t0 = time.time()
        _send(p, {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                  "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                             "clientInfo": {"name": "spendguard-probe", "version": "0"}}})
        init = _read_until(p, 1, timeout=60)
        print(f"initialize: {'OK' if init else 'NO RESPONSE'}  ({time.time()-t0:.1f}s)")
        if not init:
            return
        _send(p, {"jsonrpc": "2.0", "method": "notifications/initialized"})
        _send(p, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        tl = _read_until(p, 2, timeout=30)
        tools = (tl or {}).get("result", {}).get("tools", []) if tl else []
        print("tools:", [t.get("name") for t in tools])
        for t in tools:
            print(f"  - {t.get('name')}: {(t.get('description') or '')[:90]}")
            props = ((t.get("inputSchema") or {}).get("properties") or {})
            if props:
                print(f"      params: {list(props)[:8]}")

        # WARM LATENCY: call the `codex` tool with plugins off + read-only, timed. This is the number that decides
        # whether the mcp-server lane is worth it.
        t1 = time.time()
        _send(p, {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {
            "name": "codex", "arguments": {"prompt": "Reply with exactly: PONG", "approval-policy": "never",
                                           "sandbox": "read-only"}}})
        res = _read_until(p, 3, timeout=150)
        dt = time.time() - t1
        if not res:
            print(f"codex tool call: NO RESPONSE ({dt:.1f}s)")
            return
        result = res.get("result") or res.get("error") or {}
        content = result.get("content") if isinstance(result, dict) else None
        text = ""
        if isinstance(content, list):
            text = " ".join(c.get("text", "") for c in content if isinstance(c, dict))
        print(f"codex tool call: {dt:.1f}s  text={text[:80]!r}")
        # show the whole result shape so we learn where the threadId/conversationId lives (for codex-reply)
        print("result keys:", list(result.keys()) if isinstance(result, dict) else type(result).__name__)
        print("result (trimmed):", json.dumps(result)[:400])
    finally:
        p.terminate()


if __name__ == "__main__":
    main()
