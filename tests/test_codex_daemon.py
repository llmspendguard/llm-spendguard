"""Warm Codex lane (codex_daemon) — a persistent `codex mcp-server` reused per process, now a CONCURRENT
multiplexed JSON-RPC client (one reader thread demuxes responses by id; callers hold no lock while awaiting).

Guards (offline: no real codex):
  · _extract reads text + threadId;
  · run_warm: stateless → `codex` tool (read-only + model + reasoning, new thread); stateful → `codex-reply`;
    an MCP tool error (isError) is surfaced as ERROR not text (the codex-400-as-content bug); a dead pipe RESTARTS
    once then errors; a single call TIMEOUT fails only itself;
  · CONCURRENCY (the whole point of the rewrite): N tools/call are in flight AT ONCE over one warm daemon,
    answered OUT OF ORDER by id — wall time ≈ one delay, not N delays. Proven with a real pipe + a fake async server.
"""
import os
import sys
import json
import time
import threading
import tempfile
import concurrent.futures as cf

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
        return None

    def terminate(self):
        pass


def _canned_daemon(reply_for):
    """A fresh _CodexDaemon whose ensure_running returns a fake live proc and whose _send fulfils each tools/call
    waiter synchronously via `reply_for(args)` — so run_warm's behaviour is tested without a real server. Returns
    (daemon, sent_list). `reply_for` returns a result dict, {"error":..}, or the sentinel "__dead__"."""
    d = cd._CodexDaemon()
    sent = []
    d.ensure_running = lambda: _FakeProc()
    d.shutdown = lambda: None

    def _send(p, obj):
        sent.append(obj)
        rid = obj.get("id")
        if obj.get("method") == "tools/call" and rid is not None:
            res = reply_for(obj["params"])
            w = d._waiters.get(rid)
            if w:
                w["msg"] = {"__dead__": True} if res == "__dead__" else {"id": rid, **res}
                w["event"].set()
    d._write_rpc = _send        # stub the RPC writer (the class method is _write_rpc)
    return d, sent


print("\n-- STATELESS run → the `codex` tool, read-only + model + reasoning, returns a new thread --")
d1, sent1 = _canned_daemon(lambda a: {"result": {"structuredContent": {"content": "ANS", "threadId": "tid1"}}})
r = d1.run_warm("do X", model="openai:gpt-5.5", reasoning="low")
a = sent1[-1]["params"]
fails += ck("calls the `codex` tool", a["name"] == "codex")
fails += ck("...model stripped of provider + sandbox read-only",
            a["arguments"].get("model") == "gpt-5.5" and a["arguments"].get("sandbox") == "read-only")
fails += ck("...reasoning threaded into config (low)", (a["arguments"].get("config") or {}).get("model_reasoning_effort") == "low")
fails += ck("...returns the answer + a NEW threadId", r["text"] == "ANS" and r["thread"] == "tid1")

print("\n-- STATEFUL run (thread=…) → `codex-reply`, continuing the conversation (context) --")
d2, sent2 = _canned_daemon(lambda a: {"result": {"structuredContent": {"content": "MORE", "threadId": "tid1"}}})
r2 = d2.run_warm("and then?", thread="tid1")
a2 = sent2[-1]["params"]
fails += ck("calls `codex-reply` with the thread id", a2["name"] == "codex-reply" and a2["arguments"].get("threadId") == "tid1")

print("\n-- MCP tool ERROR (isError=true) → surfaced as ERROR, never as text (the codex-400-as-content bug) --")
_rej = "gpt-5-mini is not supported when using Codex with a ChatGPT account"
d3, _ = _canned_daemon(lambda a: {"result": {"isError": True, "content": [{"type": "text", "text": _rej}]}})
re_ = d3.run_warm("do X", model="openai:gpt-5-mini")
fails += ck("isError → text is None (not recorded as content)", re_["text"] is None)
fails += ck("isError → rejection carried on `error` verbatim", re_["error"] == _rej)
fails += ck("isError → tool_error flag set", re_.get("tool_error") is True)

print("\n-- self-heal: a dead pipe restarts ONCE then errors (never wedges) --")
d4, _ = _canned_daemon(lambda a: "__dead__")
calls = {"n": 0}
d4.ensure_running = lambda: (calls.__setitem__("n", calls["n"] + 1), _FakeProc())[1]
r3 = d4.run_warm("x")
fails += ck("restarts once then returns an error", bool(r3["error"]) and calls["n"] == 2 and r3["text"] is None)

print("\n-- CONCURRENCY: N tools/call in flight AT ONCE, answered out-of-order by id (the rewrite's whole point) --")
req_r, req_w = os.pipe()          # daemon writes requests → server reads
resp_r, resp_w = os.pipe()        # server writes responses → daemon reads
DELAY = 0.4
N = 8


class _PipeStdin:
    def write(self, b):
        os.write(req_w, b)

    def flush(self):
        pass


class _PipeProc:
    stdin = _PipeStdin()

    class _Out:
        def fileno(self):
            return resp_r
    stdout = _Out()

    def poll(self):
        return None

    def terminate(self):
        pass


def _fake_async_server():
    """Reads tools/call requests and answers each after DELAY on its OWN thread — so N in-flight requests overlap
    and responses come back out of order. This is what a concurrent server (codex ThreadManager) does."""
    buf = b""
    while True:
        try:
            chunk = os.read(req_r, 65536)
        except OSError:
            break
        if not chunk:
            break
        buf += chunk
        while b"\n" in buf:
            raw, buf = buf.split(b"\n", 1)
            raw = raw.strip()
            if not raw:
                continue
            m = json.loads(raw.decode())
            mid, method = m.get("id"), m.get("method")
            if method == "tools/call" and mid is not None:
                def _reply(mid=mid):
                    time.sleep(DELAY)
                    body = {"id": mid, "result": {"structuredContent": {"content": f"ans-{mid}", "threadId": f"t{mid}"}}}
                    os.write(resp_w, (json.dumps(body) + "\n").encode())   # small line → atomic pipe write
                threading.Thread(target=_reply, daemon=True).start()

threading.Thread(target=_fake_async_server, daemon=True).start()
dC = cd._CodexDaemon()
dC._proc = _PipeProc()
dC.ensure_running = lambda: dC._proc
threading.Thread(target=dC._reader_loop, args=(dC._proc,), daemon=True).start()

t0 = time.time()
with cf.ThreadPoolExecutor(max_workers=N) as ex:
    res = list(ex.map(lambda i: dC.run_warm(f"q{i}"), range(N)))
wall = time.time() - t0
fails += ck(f"all {N} concurrent calls answered", all(r["text"] and not r["error"] for r in res))
fails += ck("each caller got a DISTINCT answer (no cross-wiring by id — the demux is correct)",
            len({r["text"] for r in res}) == N and all(r["text"].startswith("ans-") for r in res))
fails += ck(f"CONCURRENT: wall {wall:.2f}s ≈ one {DELAY}s delay, not {N}×{DELAY}={N * DELAY:.1f}s (serialized)",
            wall < N * DELAY * 0.5)
os.close(req_r); os.close(req_w); os.close(resp_r); os.close(resp_w)

print("\n-- _spawn returns None (never raises) when the binary can't start --")
from spendguard import codex_exec as _cx                                               # noqa: E402
_o_bin, _o_flags = _cx._bin, _cx._plugin_disable_flags
try:
    _cx._bin = lambda: "/nonexistent/codex-does-not-exist"
    _cx._plugin_disable_flags = lambda: []
    fails += ck("_spawn → None on an unstartable binary", cd._CodexDaemon()._spawn() is None)
finally:
    _cx._bin, _cx._plugin_disable_flags = _o_bin, _o_flags

print("\n-- codex_exec.run_prompt swallows a run_warm EXCEPTION → error, never propagates --")
_o_daemon, _o_runwarm, _o_bin2 = _cx._daemon_enabled, cd.run_warm, _cx._bin
try:
    _cx._daemon_enabled = lambda: True

    def _boom(*a, **k):
        raise RuntimeError("boom")
    cd.run_warm = _boom
    _cx._bin = lambda: None
    _out = _cx.run_prompt("hi", model="openai:gpt-5.5")
    fails += ck("run_prompt returns an error dict (exception did not bypass the fallback)",
                isinstance(_out, dict) and bool(_out.get("error")))
finally:
    _cx._daemon_enabled, cd.run_warm, _cx._bin = _o_daemon, _o_runwarm, _o_bin2

print(f"\n{'[FAIL]' if fails else 'OK'} test_codex_daemon: {len(fails)} failure(s)")
sys.exit(1 if fails else 0)
