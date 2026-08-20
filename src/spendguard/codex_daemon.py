"""WARM Codex lane over a persistent `codex mcp-server`.

WHY. A one-shot `codex exec` COLD-STARTS every call (writable-workspace sandbox + loading all enabled plugins/MCP
servers) — MEASURED >75s, and intermittently hangs. `codex mcp-server` pays that setup ONCE at spawn; each request
is then a warm `tools/call` (MEASURED ~5s, reliable). This module holds ONE such server per process and reuses it.

LIFECYCLE (the user's ask — "starts when spendguard is there, restarted as needed"): `ensure_running()` lazily
spawns the server on first use and RESTARTS it if it died; `atexit` tears it down. Thread-safe (the pool executor is
multi-threaded), so the shared pipe is serialised under one lock.

CONTEXT (the user's ask — "maintains context on its side"): the `codex` tool returns a `threadId`; passing that back
via the `codex-reply` tool CONTINUES the same Codex conversation, so a series of delegations can build on each other.
`run(prompt)` alone is STATELESS (fresh thread each call); `run(prompt, thread=<id>)` is STATEFUL.

This is the CLIENT half; the codex lane / delegate wire to it. NB: warmth is per-PROCESS — a detached cross-process
daemon would use `codex app-server daemon` (needs the standalone-codex install); that's the documented upgrade.
"""
import atexit
import fcntl
import json
import os
import select
import subprocess
import threading
import time

from . import config

_lock = threading.RLock()
_proc = None                       # the persistent `codex mcp-server` subprocess (or None)
_rpc_id = 0
_read_buf = b""                    # bytes received but not yet split into complete lines (one persistent stdout)
STARTUP_TIMEOUT_S = 60             # the ONE-TIME server handshake budget
CALL_TIMEOUT_S = 180              # a single warm tools/call (a real task can reason for a while)


def _next_id():
    global _rpc_id
    _rpc_id += 1
    return _rpc_id


def _set_nonblocking(f):
    """Put a pipe fd in non-blocking mode so os.read never blocks past our OWN deadline. A blocking readline() could
    hang forever on a partial line the server wrote before stalling — and it holds the shared lock while it hangs."""
    fl = fcntl.fcntl(f.fileno(), fcntl.F_GETFL)
    fcntl.fcntl(f.fileno(), fcntl.F_SETFL, fl | os.O_NONBLOCK)


def _mcp_send(p, obj):
    p.stdin.write((json.dumps(obj) + "\n").encode("utf-8"))    # binary pipe (bufsize=0) — write bytes
    p.stdin.flush()


def _read_until(p, want_id, timeout):
    """Read line-delimited JSON-RPC until the response whose id==want_id; skip notifications/other ids. NEVER blocks
    past `timeout`: stdout is a NON-BLOCKING pipe, and we accumulate bytes and split lines OURSELVES. The old
    select()+readline() blocked until a newline even after the deadline (a partial line the server wrote then stalled
    on would wedge the calling thread while it held the lock), and could miss a line already sitting in a
    TextIOWrapper buffer that select() never reports readable. None on timeout or a dead pipe (the caller restarts)."""
    global _read_buf
    end = time.time() + timeout
    while True:
        while b"\n" in _read_buf:                      # drain every COMPLETE line already buffered, first
            raw, _read_buf = _read_buf.split(b"\n", 1)
            raw = raw.strip()
            if not raw:
                continue
            try:
                msg = json.loads(raw.decode("utf-8", "replace"))
            except ValueError:
                continue
            if msg.get("id") == want_id:
                return msg
        remaining = end - time.time()
        if remaining <= 0:
            return None
        if p.poll() is not None:
            return None
        r, _, _ = select.select([p.stdout], [], [], min(0.5, remaining))
        if not r:
            continue
        try:
            chunk = os.read(p.stdout.fileno(), 65536)
        except (BlockingIOError, InterruptedError):
            continue
        except (OSError, ValueError):
            return None
        if chunk == b"":                               # EOF — the server closed the pipe
            return None
        _read_buf += chunk


def _spawn():
    """Start `codex mcp-server`, do the MCP handshake, return the live process — or None if it can't come up. The
    ENTIRE handshake is guarded: if the server dies between Popen and the initialize write, the BrokenPipeError/OSError
    returns None (a startup failure the caller degrades on) rather than propagating out of ensure_running() and
    crashing the caller. Plugins are disabled at spawn (their loading is the dominant startup cost; a headless
    completion needs none)."""
    global _read_buf
    from . import codex_exec
    exe = codex_exec._bin()
    if not exe:
        return None
    env = config.lane_plan_env()      # NO metered key of ANY provider in the child — rides the ChatGPT plan login only,
    #                                   and cannot inherit ANTHROPIC_API_KEY to spend Claude tokens (no double-usage)
    cmd = [exe, "mcp-server"] + codex_exec._plugin_disable_flags()
    try:
        p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                             bufsize=0, env=env)               # binary + unbuffered → os.read on a non-blocking fd
    except Exception:
        return None
    try:
        _read_buf = b""                                       # fresh line buffer for THIS server's stream
        _set_nonblocking(p.stdout)
        rid = _next_id()                                      # CAPTURE the id — never read the global _rpc_id back
        _mcp_send(p, {"jsonrpc": "2.0", "id": rid, "method": "initialize",
                      "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                                 "clientInfo": {"name": "spendguard", "version": "1"}}})
        if _read_until(p, rid, STARTUP_TIMEOUT_S) is None:
            raise RuntimeError("no initialize response")
        _mcp_send(p, {"jsonrpc": "2.0", "method": "notifications/initialized"})
        return p
    except Exception:
        try:
            p.terminate()
        except Exception:
            pass
        return None


def ensure_running():
    """The live server, (re)started if absent or dead. Lazy auto-start + self-heal."""
    global _proc
    with _lock:
        if _proc is None or _proc.poll() is not None:
            _proc = _spawn()
    return _proc


def running():
    return _proc is not None and _proc.poll() is None


def shutdown():
    global _proc
    with _lock:
        p, _proc = _proc, None                        # detach under the lock; REAP outside it (wait() can block, and
    if p is None:                                      # holding _lock through a 5s wait would freeze every other lane)
        return
    try:
        p.terminate()
    except Exception:
        pass
    try:
        p.wait(timeout=5)                             # terminate() alone leaves an unreaped zombie on every restart;
    except Exception:                                 # a server ignoring SIGTERM stays alive — escalate to kill, then
        try:                                          # reap. Without this, repeated crashes accumulate zombie children.
            p.kill()
            p.wait(timeout=5)
        except Exception:
            pass


atexit.register(shutdown)


def _extract(result):
    """(text, threadId) from a tools/call result — text from structuredContent.content or the text blocks; threadId
    from structuredContent so a caller can CONTINUE the conversation."""
    sc = result.get("structuredContent") if isinstance(result, dict) else None
    text = ""
    if isinstance(sc, dict) and sc.get("content"):
        text = sc["content"] if isinstance(sc["content"], str) else ""
    if not text:
        content = result.get("content") if isinstance(result, dict) else None
        if isinstance(content, list):
            text = " ".join(c.get("text", "") for c in content if isinstance(c, dict)).strip()
    thread = sc.get("threadId") if isinstance(sc, dict) else None
    return text, thread


def run_warm(prompt, model=None, thread=None, reasoning=None):
    """One delegation on the WARM Codex server. `thread=None` → a fresh `codex` session (returns a new threadId);
    `thread=<id>` → `codex-reply` CONTINUES that conversation (context kept on codex's side). Returns
    {text, thread, error}. Restarts the server once on a dead pipe and retries — the lane degrades, never wedges."""
    prompt = (prompt or "")
    for attempt in (1, 2):
        p = ensure_running()
        if p is None:
            return {"text": None, "thread": None, "error": "codex mcp-server would not start"}
        if thread:
            args = {"conversationId": thread, "threadId": thread, "prompt": prompt}
            name = "codex-reply"
        else:
            args = {"prompt": prompt, "approval-policy": "never", "sandbox": "read-only"}
            if model:
                args["model"] = model.split(":", 1)[-1]
            from . import codex_exec
            eff = codex_exec._codex_effort(reasoning)
            if eff:
                args["config"] = {"model_reasoning_effort": eff}
            name = "codex"
        rid = None
        with _lock:
            try:
                rid = _next_id()
                _mcp_send(p, {"jsonrpc": "2.0", "id": rid, "method": "tools/call",
                          "params": {"name": name, "arguments": args}})
                msg = _read_until(p, rid, CALL_TIMEOUT_S)
            except (BrokenPipeError, OSError):
                msg = None
        if msg is None:                                   # dead/timed-out server → restart once, then retry
            shutdown()
            if attempt == 2:
                return {"text": None, "thread": thread, "error": "codex mcp-server call failed after restart"}
            continue
        if msg.get("error"):
            return {"text": None, "thread": thread, "error": str(msg["error"])[:200]}
        result = msg.get("result") or {}
        text, new_thread = _extract(result)
        # MCP TOOL ERROR. A tools/call result carries `isError: true` when the tool itself failed — e.g. codex
        # rejecting the model ("gpt-5-mini is not supported when using Codex with a ChatGPT account"). That error
        # text is NOT an answer; returning it as `text` is EXACTLY how a codex 400 got recorded as a $0
        # 'subscription success' and handed downstream as content (→ the caller's "missing/empty chunks"). Surface
        # it as an error so the caller falls back to the metered API. `tool_error` marks it a HARD request
        # rejection (a cold `codex exec` would hit the same wall) so run_prompt does not also pay a cold retry.
        if isinstance(result, dict) and result.get("isError"):
            return {"text": None, "thread": thread, "error": (text or "codex tool reported an error")[:200],
                    "tool_error": True}
        return {"text": text or None, "thread": new_thread or thread, "error": None if text else "empty codex reply"}
