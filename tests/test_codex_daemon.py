"""Warm Codex lane (codex_daemon) — a persistent `codex mcp-server` reused per process, so the >75s cold-start is
paid once. Guards (offline: the subprocess/IPC is mocked, no real codex): stateless uses the `codex` tool and returns
a threadId; stateful (thread=…) uses `codex-reply` to CONTINUE the conversation (context on codex's side); a
non-responding server RESTARTS once then errors rather than wedging; `_extract` reads text + threadId.
"""
import os
import sys
import tempfile

os.environ.setdefault("SPENDGUARD_HOME", tempfile.mkdtemp(prefix="sg-cdxd-"))
os.environ.setdefault("SPENDGUARD_TEST_ISOLATED", "1")
os.environ.setdefault("SPENDGUARD_NO_AUTOINSTALL", "1")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from spendguard import codex_daemon as cd                                              # noqa: E402


def ck(name, cond):
    ok = bool(cond)
    print(("  [OK] " if ok else "  [FAIL] ") + name)
    return [] if ok else [name]


fails = []

print("-- _extract pulls the answer text + the threadId (for continuation) --")
fails += ck("from structuredContent", cd._extract({"structuredContent": {"content": "HI", "threadId": "t1"}}) == ("HI", "t1"))
fails += ck("falls back to content text blocks", cd._extract({"content": [{"type": "text", "text": "HI"}]}) == ("HI", None))


class _FakeProc:
    def poll(self):
        return None                                       # always "alive" for the happy-path mocks

    def terminate(self):
        pass


sent = []
_o_ensure, _o_send, _o_read, _o_shut = cd.ensure_running, cd._mcp_send, cd._read_until, cd.shutdown
try:
    cd.ensure_running = lambda: _FakeProc()
    cd._mcp_send = lambda p, obj: sent.append(obj)
    cd.shutdown = lambda: None

    print("\n-- STATELESS run → the `codex` tool, read-only + model + reasoning, returns a new thread --")
    cd._read_until = lambda p, rid, to: {"id": rid, "result": {"structuredContent": {"content": "ANS", "threadId": "tid1"}}}
    r = cd.run_warm("do X", model="openai:gpt-5.5", reasoning="low")
    a = sent[-1]["params"]
    fails += ck("calls the `codex` tool", a["name"] == "codex")
    fails += ck("...model stripped of provider + sandbox read-only", a["arguments"].get("model") == "gpt-5.5" and a["arguments"].get("sandbox") == "read-only")
    fails += ck("...reasoning threaded into config (low)", (a["arguments"].get("config") or {}).get("model_reasoning_effort") == "low")
    fails += ck("...returns the answer + a NEW threadId", r["text"] == "ANS" and r["thread"] == "tid1")

    print("\n-- STATEFUL run (thread=…) → `codex-reply`, continuing the conversation (context) --")
    r2 = cd.run_warm("and then?", thread="tid1")
    a2 = sent[-1]["params"]
    fails += ck("calls `codex-reply` with the thread id", a2["name"] == "codex-reply" and a2["arguments"].get("threadId") == "tid1")

    print("\n-- self-heal: a non-responding server restarts ONCE then errors (never wedges) --")
    calls = {"n": 0}

    def _ensure_count():
        calls["n"] += 1
        return _FakeProc()
    cd.ensure_running = _ensure_count
    cd._read_until = lambda p, rid, to: None              # simulate a dead/timed-out server every time
    r3 = cd.run_warm("x")
    fails += ck("restarts once then returns an error", bool(r3["error"]) and calls["n"] == 2 and r3["text"] is None)
finally:
    cd.ensure_running, cd._mcp_send, cd._read_until, cd.shutdown = _o_ensure, _o_send, _o_read, _o_shut
    cd._proc = None

print(f"\n{'[FAIL]' if fails else 'OK'} test_codex_daemon: {len(fails)} failure(s)")
sys.exit(1 if fails else 0)
