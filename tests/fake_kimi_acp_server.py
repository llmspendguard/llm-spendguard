#!/usr/bin/env python3
"""A minimal fake `kimi acp` ACP server for offline daemon tests — speaks just enough ACP (initialize → session/new →
session/prompt with streamed agent_message_chunk + a stopReason response) to exercise kimi_daemon without a real CLI
or any model spend. Each boot appends a line to $FAKE_KIMI_BOOT_LOG so a test can PROVE one cold-start serves N
prompts. Turns run on their own threads so concurrent prompts demux and one wedged prompt never blocks the others.

Special prompts: 'wedge' never completes (no response); 'refuse' returns stopReason=refusal with no text; 'permreq'
first sends a SERVER→CLIENT request and only completes after the client replies (so the no-hang reply path is tested).
"""
import json
import os
import sys
import threading
import time

write_lock = threading.Lock()
next_session = 0
server_request_replies = {}        # rid -> Event, set when the client answers a server-initiated request


def send(message):
    with write_lock:
        sys.stdout.write(json.dumps(message) + "\n")
        sys.stdout.flush()


def finish_prompt(session_id, rid, text):
    time.sleep(0.15 if text == "slow" else 0.02)
    if text == "wedge":
        return                                             # never respond — a wedged turn
    if text == "refuse":
        send({"jsonrpc": "2.0", "id": rid, "result": {"stopReason": "refusal"}})
        return
    if text == "permreq":
        server_rid = 900000 + rid
        ev = threading.Event()
        server_request_replies[server_rid] = ev
        send({"jsonrpc": "2.0", "id": server_rid, "method": "session/request_permission",
              "params": {"sessionId": session_id}})
        if not ev.wait(3):                                 # the client must reply or we'd hang — the daemon declines it
            return
    # a reasoning chunk (must be IGNORED by the client) then the answer in two chunks (tests concatenation)
    send({"jsonrpc": "2.0", "method": "session/update", "params": {
        "sessionId": session_id, "update": {"sessionUpdate": "agent_thought_chunk",
                                             "content": {"type": "text", "text": "(thinking)"}}}})
    for part in ("reply:", text):
        send({"jsonrpc": "2.0", "method": "session/update", "params": {
            "sessionId": session_id, "update": {"sessionUpdate": "agent_message_chunk",
                                                "content": {"type": "text", "text": part}}}})
    send({"jsonrpc": "2.0", "id": rid, "result": {"stopReason": "end_turn"}})


if len(sys.argv) < 2 or sys.argv[1] != "acp":
    raise SystemExit(2)
_boot_log = os.environ.get("FAKE_KIMI_BOOT_LOG")
if _boot_log:
    with open(_boot_log, "a") as fh:
        fh.write("BOOT %d\n" % os.getpid())

for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    message = json.loads(line)
    method = message.get("method")
    if method is None:                                     # a response to a server-initiated request (e.g. permreq)
        ev = server_request_replies.get(message.get("id"))
        if ev is not None:
            ev.set()
        continue
    if method == "initialize":
        assert message["params"]["protocolVersion"] == 1
        send({"jsonrpc": "2.0", "id": message["id"], "result": {
            "protocolVersion": 1, "agentCapabilities": {}, "authMethods": []}})
    elif method == "session/new":
        next_session += 1
        send({"jsonrpc": "2.0", "id": message["id"], "result": {"sessionId": f"sess-{next_session}"}})
    elif method == "session/prompt":
        params = message["params"]
        prompt_text = params["prompt"][0]["text"]
        threading.Thread(target=finish_prompt,
                         args=(params["sessionId"], message["id"], prompt_text), daemon=True).start()
    elif method == "fake/exit":
        os._exit(0)
