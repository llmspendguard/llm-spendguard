"""Warm Codex lane (codex_daemon) — a persistent `codex mcp-server` reused per process, so the >75s cold-start is
paid once. Guards (offline: the subprocess/IPC is mocked, no real codex): stateless uses the `codex` tool and returns
a threadId; stateful (thread=…) uses `codex-reply` to CONTINUE the conversation (context on codex's side); a
non-responding server RESTARTS once then errors rather than wedging; `_extract` reads text + threadId.
"""
import os
import sys
import tempfile
import time

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

    print("\n-- MCP tool ERROR (isError=true) → surfaced as ERROR, never as text (the codex-400-as-content bug) --")
    _rej = "gpt-5-mini is not supported when using Codex with a ChatGPT account"
    cd._read_until = lambda p, rid, to: {"id": rid, "result": {"isError": True,
                                                               "content": [{"type": "text", "text": _rej}]}}
    re_ = cd.run_warm("do X", model="openai:gpt-5-mini")
    fails += ck("isError → text is None (the tool result is NOT recorded as content)", re_["text"] is None)
    fails += ck("isError → the rejection text is carried through on `error` verbatim", re_["error"] == _rej)
    fails += ck("isError → tool_error flag set (a cold exec would hit the same wall)", re_.get("tool_error") is True)

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


# ── the REAL IO layer (no stubs): the honestreview-found robustness bugs ───────────────────────────────
print("\n-- _read_until on a REAL non-blocking pipe: parses complete lines, skips other ids --")
r_fd, w_fd = os.pipe()
_fl = cd.fcntl.fcntl(r_fd, cd.fcntl.F_GETFL)
cd.fcntl.fcntl(r_fd, cd.fcntl.F_SETFL, _fl | os.O_NONBLOCK)


class _RealP:                                          # minimal `p`: a non-blocking read fd + an alive poll()
    class _Out:
        def fileno(self_inner):
            return r_fd
    stdout = _Out()

    def poll(self):
        return None


_p = _RealP()
cd._read_buf = b""
os.write(w_fd, b'{"id":1,"result":"skip"}\n{"id":2,"result":"want"}\n')
_m = cd._read_until(_p, 2, timeout=2)
fails += ck("returns the matching id, skipping earlier ones", _m is not None and _m.get("id") == 2)

print("\n-- _read_until does NOT hang on a partial line — respects the deadline (the 52/56 finding) --")
cd._read_buf = b""
os.write(w_fd, b'{"id":3,"result":"partial')            # NO newline: a stalled partial write must not wedge us
_t0 = time.time()
_m2 = cd._read_until(_p, 3, timeout=1)
_dt = time.time() - _t0
fails += ck("returns None ~by the deadline, never blocks forever", _m2 is None and _dt < 3)
os.close(r_fd)
os.close(w_fd)

print("\n-- _spawn returns None (never raises) when the server binary can't start (the 81/85 finding) --")
from spendguard import codex_exec as _cx                                               # noqa: E402
_o_bin, _o_flags = _cx._bin, _cx._plugin_disable_flags
try:
    _cx._bin = lambda: "/nonexistent/codex-does-not-exist"
    _cx._plugin_disable_flags = lambda: []
    fails += ck("_spawn → None on an unstartable binary", cd._spawn() is None)
finally:
    _cx._bin, _cx._plugin_disable_flags = _o_bin, _o_flags

print("\n-- codex_exec.run_prompt swallows a run_warm EXCEPTION → error, never propagates (the 135 finding) --")
_o_daemon, _o_runwarm, _o_bin2 = _cx._daemon_enabled, cd.run_warm, _cx._bin
try:
    _cx._daemon_enabled = lambda: True

    def _boom(*a, **k):
        raise RuntimeError("boom")
    cd.run_warm = _boom
    _cx._bin = lambda: None                            # exec fallback finds no codex → returns {error}, no crash
    _out = _cx.run_prompt("hi", model="openai:gpt-5.5")
    fails += ck("run_prompt returns an error dict (exception did not bypass the fallback)",
                isinstance(_out, dict) and bool(_out.get("error")))
finally:
    _cx._daemon_enabled, cd.run_warm, _cx._bin = _o_daemon, _o_runwarm, _o_bin2

print(f"\n{'[FAIL]' if fails else 'OK'} test_codex_daemon: {len(fails)} failure(s)")
sys.exit(1 if fails else 0)
