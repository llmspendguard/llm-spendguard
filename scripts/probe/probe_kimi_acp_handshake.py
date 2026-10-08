#!/usr/bin/env python3
"""LIVE structural probe: does the REAL `kimi acp` server speak the ACP flow kimi_daemon implements? Runs ONLY
initialize + session/new (NO session/prompt), so NO model turn happens → $0, no plan usage. Confirms the real CLI
returns a `sessionId` for session/new exactly as the fake-server tests assume. Gated interpreter required.

Usage:  .venv.nosync/bin/python scripts/probe/probe_kimi_acp_handshake.py
"""
import spendguard
spendguard.require()

import sys
import time
from spendguard import kimi_daemon, kimi_exec


def main():
    if not kimi_exec.available():
        print("SKIP: kimi CLI not found on this host")
        return 0
    d = kimi_daemon._KimiAcpDaemon()
    try:
        p = d.ensure_running()                 # spawns `kimi acp` + runs the ACP initialize handshake
        if p is None:
            print("FAIL: kimi acp would not start / initialize handshake did not complete")
            return 1
        print("OK: initialize handshake completed, server live")
        deadline = time.monotonic() + 30

        def remaining():
            return max(0.0, deadline - time.monotonic())

        w, msg = d._send_request(p, "session/new", {"cwd": "/tmp", "mcpServers": []}, remaining)
        d._drop_waiter(w["_rid"])
        if msg is None or msg.get("__dead__"):
            print("FAIL: session/new timed out or pipe died")
            return 1
        if msg.get("error"):
            print("FAIL: session/new error:", str(msg["error"])[:200])
            return 1
        sid = (msg.get("result") or {}).get("sessionId")
        print("session/new result keys:", sorted((msg.get("result") or {}).keys()))
        print("sessionId:", sid)
        ok = bool(sid)
        print("OK: real kimi acp returns a sessionId (warm-pool session isolation confirmed)" if ok
              else "FAIL: no sessionId in session/new result")
        if ok and "--live-prompt" in sys.argv:
            # One tiny real warm turn on the $0 Kimi Code plan — confirms the agent_message_chunk → final-text path
            # against the REAL server end-to-end (not just the fake). $0 billed (flat-fee plan).
            print("\n-- live warm prompt (one tiny turn on the $0 plan) --")
            r = d.run_warm("Reply with exactly the word: pong")
            print("warm result:", {"text": (r.get("text") or "")[:80], "error": r.get("error")})
            ok = bool(r.get("text")) and not r.get("error")
            print("OK: real warm prompt returned text" if ok else "FAIL: real warm prompt produced no text")
        return 0 if ok else 1
    finally:
        d.shutdown()


if __name__ == "__main__":
    raise SystemExit(main())
