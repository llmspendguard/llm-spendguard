#!/usr/bin/env python3
import json
import os
import sys
import threading
import time


write_lock = threading.Lock()
next_thread = 0
next_turn = 0


def send(message):
    with write_lock:
        sys.stdout.write(json.dumps(message) + "\n")
        sys.stdout.flush()


def finish_turn(thread_id, turn_id, prompt):
    time.sleep(0.15 if prompt == "first" else 0.02)
    status = "failed" if prompt == "fail" else "completed"
    if status == "completed":
        send({"jsonrpc": "2.0", "method": "item/completed", "params": {
            "threadId": thread_id, "turnId": turn_id, "completedAtMs": 1,
            "item": {"type": "agentMessage", "id": "item-" + turn_id, "text": "reply:" + prompt}}})
    turn = {"id": turn_id, "status": status, "items": []}
    if status == "failed":
        turn["error"] = {"message": "fake failure"}
    send({"jsonrpc": "2.0", "method": "turn/completed",
          "params": {"threadId": thread_id, "turn": turn}})


if len(sys.argv) < 2 or sys.argv[1] != "app-server":
    raise SystemExit(2)

for line in sys.stdin:
    message = json.loads(line)
    method = message.get("method")
    if method == "initialize":
        assert message["params"]["clientInfo"] == {
            "name": "spendguard", "title": "spendguard", "version": "1"}
        send({"jsonrpc": "2.0", "id": message["id"], "result": {}})
    elif method == "initialized":
        assert message.get("params") == {}
    elif method == "thread/start":
        assert message["params"]["approvalPolicy"] == "never"
        assert message["params"]["sandbox"] in ("read-only", "workspace-write")
        next_thread += 1
        thread_id = f"thread-{next_thread}"
        send({"jsonrpc": "2.0", "id": message["id"], "result": {"thread": {"id": thread_id}}})
    elif method == "thread/resume":
        thread_id = message["params"]["threadId"]
        send({"jsonrpc": "2.0", "id": message["id"], "result": {"thread": {"id": thread_id}}})
    elif method == "turn/start":
        next_turn += 1
        turn_id = f"turn-{next_turn}"
        params = message["params"]
        assert params["approvalPolicy"] == "never"
        assert params["sandboxPolicy"]["type"] in ("readOnly", "workspaceWrite")
        prompt = params["input"][0]["text"]
        send({"jsonrpc": "2.0", "id": message["id"],
              "result": {"turn": {"id": turn_id, "status": "inProgress", "items": []}}})
        threading.Thread(target=finish_turn, args=(params["threadId"], turn_id, prompt), daemon=True).start()
    elif method == "fake/exit":
        os._exit(0)
