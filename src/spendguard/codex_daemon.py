"""WARM Codex lane over a persistent `codex mcp-server` — CONCURRENT (multiplexed JSON-RPC).

WHY. A one-shot `codex exec` COLD-STARTS every call (writable-workspace sandbox + loading all enabled plugins/MCP
servers) — MEASURED >75s, and intermittently hangs. `codex mcp-server` pays that setup ONCE at spawn; each request
is then a warm `tools/call` (MEASURED ~5s). This module holds ONE such server per process and reuses it.

CONCURRENCY (the fix). The mcp-server is an ASYNC JSON-RPC server (codex-rs: Tokio stdin-reader / processor /
stdout-writer; `ThreadManager` + `ActiveTurnRegistry` spawn a Codex session PER tools/call and answer them
out-of-order by `id`). The old client threw that away: it held ONE lock across the whole send→read round-trip, so
only a single tools/call was ever in flight and N concurrent delegations SERIALISED (measured: per-call latency 8x
under a 16-way fan, ~1x speedup). Now a single background READER thread demuxes every response to the waiting caller
by `id`; a caller locks only for the (fast) stdin WRITE, then waits on its OWN event holding no lock — so N turns
are in flight at once over the one warm daemon, exactly what the server supports (measured ceiling: the ChatGPT
plan, not us).

The state lives on ONE `_CodexDaemon` instance (a class, so the concurrency invariants are encapsulated, not module
globals); the module-level functions are thin delegators kept for the existing callers. `ensure_running()` lazily
spawns the server (serialised so two threads never create two) and RESTARTS it on death; `atexit` tears it down.
CONTEXT: the `codex` tool returns a `threadId`; passing it back via `codex-reply` CONTINUES the conversation.
A detached cross-process daemon would use `codex app-server daemon` — the documented upgrade.
"""
import atexit
import fcntl
import itertools
import json
import os
import select
import subprocess
import tempfile
import threading

from . import config

STARTUP_TIMEOUT_S = 60             # the ONE-TIME server handshake budget
CALL_TIMEOUT_S = 180              # a single warm tools/call (a real task can reason for a while)
# A FIXED, non-external working directory for the headless read-only server (it does no file work for meta prompts).
# A constant cwd is the contained-spawn form: the spawn's directory can never be steered by any caller's input.
_SAFE_CWD = tempfile.gettempdir()


class _CodexDaemon:
    """One warm `codex mcp-server` + a concurrent JSON-RPC client over it. All mutable state is on the instance
    (guarded by the instance locks), so N callers run concurrent turns without a serialising round-trip lock."""

    def __init__(self):
        self._state_lock = threading.Lock()    # guards _proc lifecycle + the _waiters registry
        self._spawn_lock = threading.Lock()    # serialises SPAWNS (a slow handshake must not run under _state_lock)
        self._write_lock = threading.Lock()    # serialises stdin WRITES only — never held while awaiting a response
        self._proc = None
        self._waiters = {}                     # rpc_id -> {"event","msg"} — one per in-flight request (instance state)
        self._ids = itertools.count(1)         # itertools.count.__next__ is atomic under the GIL

    def _next_id(self):
        return next(self._ids)

    def _register_waiter(self, rid):
        w = {"event": threading.Event(), "msg": None}
        with self._state_lock:
            self._waiters[rid] = w
        return w

    def _drop_waiter(self, rid):
        with self._state_lock:
            self._waiters.pop(rid, None)

    def _fail_all_waiters(self):
        """A dead/closed pipe: wake EVERY pending caller with a dead marker so none hangs to its full timeout."""
        with self._state_lock:
            pending = list(self._waiters.values())
        for w in pending:
            if w["msg"] is None:
                w["msg"] = {"__dead__": True}
            w["event"].set()

    def _write_rpc(self, p, obj):
        """Write one JSON-RPC message under the write lock (held only for the write, never across the response wait)."""
        line = (json.dumps(obj) + "\n").encode("utf-8")
        with self._write_lock:
            p.stdin.write(line)
            p.stdin.flush()

    def _reader_loop(self, p):
        """ONE per-proc thread: read line-delimited JSON-RPC off the shared stdout and hand each response to its
        waiter by `id`. The SOLE reader of the pipe — no shared read-buffer to lock, no cross-caller response theft.
        Notifications (no id) are ignored. Exits on EOF / dead pipe / read error, failing all waiters."""
        buf = b""
        try:
            while True:
                if p.poll() is not None:
                    break
                try:
                    r, _, _ = select.select([p.stdout], [], [], 0.5)
                except (OSError, ValueError):
                    break
                if not r:
                    continue
                try:
                    chunk = os.read(p.stdout.fileno(), 65536)
                except (BlockingIOError, InterruptedError):
                    continue
                except (OSError, ValueError):
                    break
                if chunk == b"":                           # EOF — server closed the pipe
                    break
                buf += chunk
                while b"\n" in buf:
                    raw, buf = buf.split(b"\n", 1)
                    raw = raw.strip()
                    if not raw:
                        continue
                    try:
                        msg = json.loads(raw.decode("utf-8", "replace"))
                    except ValueError:
                        continue
                    mid = msg.get("id")
                    if mid is None:                        # a notification (streamed event) — no waiter to satisfy
                        continue
                    with self._state_lock:
                        w = self._waiters.get(mid)
                    if w is not None:
                        w["msg"] = msg
                        w["event"].set()
        finally:
            self._fail_all_waiters()

    def _spawn(self):
        """Start `codex mcp-server`, START THE READER, handshake through the multiplexer; return the live process
        or None (a startup failure the caller degrades on — the lane may degrade, the advisor may not break). Called
        only under _spawn_lock, so there is never a concurrent second spawn."""
        from . import codex_exec
        exe = codex_exec._bin()
        if not exe:
            config.warn_once("[spendguard] codex warm daemon: the codex CLI is not found — the codex lane is "
                             "unavailable this run and callers fall back to the metered API")
            return None
        # CONTAINMENT: spawn only a real, executable, symlink-RESOLVED binary (config.resolve_cli's pin→PATH→
        # well-known dirs — the same trusted CLI codex_exec spawns) with a CONSTANT cwd (_SAFE_CWD). Neither the
        # executable nor the working directory is influenced by any caller's input.
        exe = os.path.realpath(exe)
        if not (os.path.isfile(exe) and os.access(exe, os.X_OK)):
            config.warn_once("[spendguard] codex warm daemon: resolved codex path %r is not an executable file "
                             "— codex lane unavailable, falling back to the metered API" % exe)
            return None
        cmd = [exe, "mcp-server"] + codex_exec._plugin_disable_flags()
        try:
            p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                 bufsize=0, cwd=_SAFE_CWD, env=config.lane_plan_env())   # no metered key in the child
        except Exception:
            return None
        fl = fcntl.fcntl(p.stdout.fileno(), fcntl.F_GETFL)     # non-blocking stdout for the reader's os.read
        fcntl.fcntl(p.stdout.fileno(), fcntl.F_SETFL, fl | os.O_NONBLOCK)
        threading.Thread(target=self._reader_loop, args=(p,), daemon=True).start()   # the sole reader for THIS proc
        rid = self._next_id()                                  # initialize is itself a multiplexed request
        w = self._register_waiter(rid)
        ok = False
        try:
            self._write_rpc(p, {"jsonrpc": "2.0", "id": rid, "method": "initialize",
                           "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                                      "clientInfo": {"name": "spendguard", "version": "1"}}})
            ok = w["event"].wait(STARTUP_TIMEOUT_S) and bool(w["msg"]) and not w["msg"].get("__dead__")
        except (BrokenPipeError, OSError):
            ok = False
        finally:
            self._drop_waiter(rid)
        if not ok:
            config.warn_once("[spendguard] codex warm daemon: no initialize response within %ds — treating the "
                             "server as unavailable this run (falling back to the metered API)" % STARTUP_TIMEOUT_S)
            self._terminate(p)
            return None
        try:
            self._write_rpc(p, {"jsonrpc": "2.0", "method": "notifications/initialized"})
        except (BrokenPipeError, OSError):
            self._terminate(p)
            return None
        return p

    @staticmethod
    def _terminate(p):
        try:
            p.terminate()
        except Exception:
            pass

    def ensure_running(self):
        """The live server, (re)started if absent or dead. Spawns are SERIALISED under _spawn_lock with a re-check,
        so two concurrent callers never create two servers (one would leak)."""
        with self._state_lock:
            if self._proc is not None and self._proc.poll() is None:
                return self._proc
        with self._spawn_lock:                        # only one spawner at a time
            with self._state_lock:                    # re-check: another thread may have spawned while we waited
                if self._proc is not None and self._proc.poll() is None:
                    return self._proc
            p = self._spawn()                         # slow handshake — outside _state_lock, inside _spawn_lock
            with self._state_lock:
                self._proc = p
            return p

    def running(self):
        return self._proc is not None and self._proc.poll() is None

    def shutdown(self):
        with self._state_lock:
            p, self._proc = self._proc, None          # detach under the lock; REAP outside it (wait() can block)
        if p is None:
            return
        self._terminate(p)
        try:
            p.wait(timeout=5)                         # terminate() alone leaves a zombie on every restart; escalate
        except Exception:                             # to kill + reap so repeated crashes don't accumulate children
            try:
                p.kill()
                p.wait(timeout=5)
            except Exception:
                pass
        self._fail_all_waiters()                      # anything still pending on the dead proc is woken, not hung

    def run_warm(self, prompt, model=None, thread=None, reasoning=None):
        """One delegation on the WARM server, CONCURRENCY-SAFE — many callers in flight, each waiting on its OWN
        response by id, holding no lock. A single call that TIMES OUT fails only ITSELF (the caller falls back to
        the metered API); only a genuinely dead pipe restarts the server, so one slow turn never sinks the others."""
        prompt = (prompt or "")
        for attempt in (1, 2):
            p = self.ensure_running()
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
            rid = self._next_id()
            w = self._register_waiter(rid)
            try:
                try:
                    self._write_rpc(p, {"jsonrpc": "2.0", "id": rid, "method": "tools/call",
                                   "params": {"name": name, "arguments": args}})
                except (BrokenPipeError, OSError):
                    w["msg"] = {"__dead__": True}
                    w["event"].set()
                got = w["event"].wait(CALL_TIMEOUT_S)         # wait on OUR response only — NO lock held here
                msg = w["msg"] if got else None
            finally:
                self._drop_waiter(rid)
            if msg is not None and msg.get("__dead__"):       # the pipe died under us → restart once, then retry
                self.shutdown()
                if attempt == 2:
                    config.warn_once("[spendguard] codex warm daemon: a call still failed after a restart — the "
                                     "lane is degrading to the metered API this run (not the advisor breaking)")
                    return {"text": None, "thread": thread, "error": "codex mcp-server call failed after restart"}
                continue
            if msg is None:                                   # THIS call timed out but the pipe is alive → fail only IT
                config.warn_once("[spendguard] codex warm daemon: a warm call exceeded %ds — failing that ONE call "
                                 "to the metered API (the server stays up for the others)" % CALL_TIMEOUT_S)
                return {"text": None, "thread": thread, "error": f"codex warm call timeout ({CALL_TIMEOUT_S}s)"}
            if msg.get("error"):
                return {"text": None, "thread": thread, "error": str(msg["error"])[:200]}
            result = msg.get("result") or {}
            text, new_thread = _extract(result)
            # MCP TOOL ERROR: `isError: true` means the tool itself failed (e.g. codex rejecting the model). That text
            # is NOT an answer; return it as an error so the caller falls back to the metered API. `tool_error` marks
            # a HARD request rejection (a cold `codex exec` would hit the same wall) so run_prompt skips a cold retry.
            if isinstance(result, dict) and result.get("isError"):
                return {"text": None, "thread": thread, "error": (text or "codex tool reported an error")[:200],
                        "tool_error": True}
            return {"text": text or None, "thread": new_thread or thread,
                    "error": None if text else "empty codex reply"}


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


# ── ONE per-process daemon instance; the module API is the singleton's BOUND METHODS (assignment, not a second
# `def` — so the public names delegate without duplicating the class methods' definitions) ──
_DAEMON = _CodexDaemon()
run_warm = _DAEMON.run_warm            # the existing lane/callers' API (codex_exec.run_prompt → codex_daemon.run_warm)
ensure_running = _DAEMON.ensure_running
running = _DAEMON.running
shutdown = _DAEMON.shutdown

atexit.register(shutdown)
