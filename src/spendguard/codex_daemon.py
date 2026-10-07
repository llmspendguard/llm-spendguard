"""WARM Codex lane over a persistent `codex app-server` — CONCURRENT (multiplexed JSON-RPC).

WHY. A one-shot `codex exec` COLD-STARTS every call (writable-workspace sandbox + loading all enabled plugins/MCP
servers) — MEASURED >75s, and intermittently hangs. `codex app-server` pays that setup ONCE at spawn; each request
is then a warm thread turn. This module holds ONE such server per process and reuses it.

CONCURRENCY. A single background READER thread demuxes request responses by `id` and turn notifications by
(`threadId`, `turnId`). A caller locks only for the fast stdin WRITE, then waits on its OWN event holding no lock,
so N turns remain in flight at once over the one warm daemon.

The state lives on ONE `_CodexDaemon` instance (a class, so the concurrency invariants are encapsulated, not module
globals); the module-level functions are thin delegators kept for the existing callers. `ensure_running()` lazily
spawns the server (serialised so two threads never create two) and RESTARTS it on death; `atexit` tears it down.
CONTEXT: `thread/start` returns a thread id; passing it back through `thread/resume` CONTINUES the conversation.
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
import time

from . import config

STARTUP_TIMEOUT_S = 60             # the ONE-TIME server handshake budget
CALL_TIMEOUT_S = 180              # one warm turn, including setup + streamed completion
IDLE_TIMEOUT_S = 600              # reclaim the persistent subprocess after ten minutes with no completed/new call
# A FIXED, non-external bootstrap directory for the server process. Each tool call carries its explicit task cwd;
# keeping the daemon spawn itself here means its startup directory can never be steered by prompt content.
_SAFE_CWD = tempfile.gettempdir()


def _mcp_disable_flags():
    """Disable MCP orchestration globally for this headless app-server process.

    Codex 0.160.1's config explicitly gates configured servers on ``orchestrator.mcp.enabled``. Empty-table and
    per-entry overrides are not equivalent: config layers merge the former, while the latter can replace required
    transport fields and make app-server exit before the initialize handshake.
    """
    return ["-c", "orchestrator.mcp.enabled=false"]


class _CodexDaemon:
    """One warm `codex app-server` + a concurrent JSON-RPC client over it. All mutable state is on the instance
    (guarded by the instance locks), so N callers run concurrent turns without a serialising round-trip lock."""

    def __init__(self):
        self._state_lock = threading.Lock()    # guards _proc lifecycle + the _waiters registry
        self._spawn_lock = threading.Lock()    # serialises SPAWNS (a slow handshake must not run under _state_lock)
        self._write_lock = threading.Lock()    # serialises stdin WRITES only — never held while awaiting a response
        self._proc = None
        self._idle_timer = None
        self._last_activity = 0.0
        self._waiters = {}                     # rpc_id -> request waiter
        self._turn_waiters = {}                # (thread_id, turn_id) -> completion waiter
        self._pending_turn_notifications = {}  # notification can race ahead of the turn/start response
        self._ids = itertools.count(1)         # itertools.count.__next__ is atomic under the GIL

    def _next_id(self):
        return next(self._ids)

    def _cancel_idle_shutdown(self):
        with self._state_lock:
            timer, self._idle_timer = self._idle_timer, None
        if timer is not None:
            timer.cancel()

    def _schedule_idle_shutdown(self):
        """Reset the one-shot idle timer after a call. The timestamp check makes a stale callback harmless."""
        self._cancel_idle_shutdown()
        activity = time.monotonic()
        with self._state_lock:
            self._last_activity = activity

        def _shutdown_if_still_idle():
            with self._state_lock:
                still_idle = self._last_activity == activity
                if still_idle:
                    self._idle_timer = None
            if still_idle:
                self.shutdown()

        timer = threading.Timer(IDLE_TIMEOUT_S, _shutdown_if_still_idle)
        timer.daemon = True
        with self._state_lock:
            self._idle_timer = timer
        timer.start()

    def _register_waiter(self, rid, turn_thread_id=None):
        w = {"event": threading.Event(), "msg": None, "turn_event": threading.Event(),
             "turn_thread_id": turn_thread_id, "turn_id": None, "texts": [], "turn": None, "dead": False}
        with self._state_lock:
            self._waiters[rid] = w
        return w

    def _drop_waiter(self, rid):
        with self._state_lock:
            self._waiters.pop(rid, None)

    def _drop_turn_waiter(self, waiter):
        key = (waiter.get("turn_thread_id"), waiter.get("turn_id"))
        with self._state_lock:
            if self._turn_waiters.get(key) is waiter:
                self._turn_waiters.pop(key, None)

    def _fail_all_waiters(self):
        """A dead/closed pipe: wake EVERY pending caller with a dead marker so none hangs to its full timeout."""
        with self._state_lock:
            pending = list({id(w): w for w in [*self._waiters.values(), *self._turn_waiters.values()]}.values())
        for w in pending:
            w["dead"] = True
            if w["msg"] is None:
                w["msg"] = {"__dead__": True}
            w["event"].set()
            w["turn_event"].set()

    def _write_rpc(self, p, obj):
        """Write one JSON-RPC message under the write lock (held only for the write, never across the response wait)."""
        line = (json.dumps(obj) + "\n").encode("utf-8")
        with self._write_lock:
            p.stdin.write(line)
            p.stdin.flush()

    def _route_notification(self, msg):
        method = msg.get("method")
        if method not in ("item/completed", "turn/completed"):
            return
        params = msg.get("params") or {}
        turn = params.get("turn") or {}
        turn_id = params.get("turnId") or turn.get("id")
        key = (params.get("threadId"), turn_id)
        with self._state_lock:
            waiter = self._turn_waiters.get(key)
            if waiter is None:
                # A tiny turn can complete before this reader handles the turn/start response that reveals its id.
                # Retain the complete evidence and replay it as soon as correlation exists instead of dropping it.
                self._pending_turn_notifications.setdefault(key, []).append(msg)
                return
            if method == "item/completed":
                item = params.get("item") or {}
                if item.get("type") == "agentMessage" and isinstance(item.get("text"), str):
                    waiter["texts"].append(item["text"])
            else:
                waiter["turn"] = turn
                waiter["turn_event"].set()

    def _correlate_turn_waiter(self, waiter, thread_id, turn_id):
        """Install turn correlation and replay real notifications that won the response/notification race."""
        key = (thread_id, turn_id)
        with self._state_lock:
            waiter["turn_id"] = turn_id
            self._turn_waiters[key] = waiter
            pending = self._pending_turn_notifications.pop(key, [])
        for notification in pending:
            self._route_notification(notification)

    def _reader_loop(self, p):
        """SOLE stdout reader: route responses by id and notifications by (threadId, turnId)."""
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
                    if mid is None:
                        self._route_notification(msg)
                        continue
                    with self._state_lock:
                        w = self._waiters.get(mid)
                        if w is not None:
                            w["msg"] = msg
                            turn = (msg.get("result") or {}).get("turn") or {}
                    if w is not None:
                        if w["turn_thread_id"] and turn.get("id"):
                            self._correlate_turn_waiter(w, w["turn_thread_id"], turn["id"])
                        w["event"].set()
        finally:
            self._fail_all_waiters()

    def _spawn(self):
        """Start `codex app-server`, START THE READER, handshake through the multiplexer; return the live process
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
        cmd = [exe, "app-server"] + codex_exec._plugin_disable_flags() + _mcp_disable_flags()
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
                           "params": {"clientInfo": {"name": "spendguard", "title": "spendguard",
                                                     "version": "1"}}})
            ok = (w["event"].wait(STARTUP_TIMEOUT_S) and bool(w["msg"])
                  and not w["msg"].get("__dead__") and not w["msg"].get("error"))
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
            self._write_rpc(p, {"jsonrpc": "2.0", "method": "initialized", "params": {}})
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
        self._cancel_idle_shutdown()
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

    def run_warm(self, prompt, model=None, thread=None, reasoning=None, sandbox="read-only", cwd=None):
        """One delegation on the WARM server, CONCURRENCY-SAFE — many callers in flight, each waiting on its OWN
        response by id, holding no lock. A single call that TIMES OUT fails only ITSELF (the caller falls back to
        the metered API); only a genuinely dead pipe restarts the server, so one slow turn never sinks the others."""
        prompt = (prompt or "")
        if sandbox not in ("read-only", "workspace-write"):
            return {"text": None, "thread": thread, "error": f"unsupported codex sandbox {sandbox!r}"}
        self._cancel_idle_shutdown()
        try:
            for attempt in (1, 2):
                p = self.ensure_running()
                if p is None:
                    return {"text": None, "thread": None, "error": "codex app-server would not start"}
                deadline = time.monotonic() + CALL_TIMEOUT_S

                def remaining_timeout(call_deadline=deadline):
                    return max(0.0, call_deadline - time.monotonic())

                task_cwd = os.path.abspath(cwd or _SAFE_CWD)
                bare_model = model.split(":", 1)[-1] if model else None
                approval_policy = "never"
                if thread:
                    thread_method = "thread/resume"
                    thread_params = {"threadId": thread}
                else:
                    thread_method = "thread/start"
                    thread_params = {"cwd": task_cwd, "approvalPolicy": approval_policy, "sandbox": sandbox}
                    if bare_model:
                        thread_params["model"] = bare_model
                rid = self._next_id()
                w = self._register_waiter(rid)
                try:
                    try:
                        self._write_rpc(p, {"jsonrpc": "2.0", "id": rid, "method": thread_method,
                                            "params": thread_params})
                    except (BrokenPipeError, OSError):
                        w["dead"] = True
                        w["msg"] = {"__dead__": True}
                        w["event"].set()
                    got = w["event"].wait(remaining_timeout())
                    msg = w["msg"] if got else None
                finally:
                    self._drop_waiter(rid)
                if msg is not None and msg.get("__dead__"):       # the pipe died under us → restart once, then retry
                    self.shutdown()
                    if attempt == 2:
                        config.warn_once("[spendguard] codex warm daemon: a call still failed after a restart — the "
                                         "lane is degrading to the metered API this run (not the advisor breaking)")
                        return {"text": None, "thread": thread, "error": "codex app-server call failed after restart"}
                    continue
                if msg is None:                                   # THIS call timed out but the pipe is alive → fail only IT
                    config.warn_once("[spendguard] codex warm daemon: a warm call exceeded %ds — failing that ONE call "
                                     "to the metered API (the server stays up for the others)" % CALL_TIMEOUT_S)
                    return {"text": None, "thread": thread, "error": f"codex warm call timeout ({CALL_TIMEOUT_S}s)"}
                if msg.get("error"):
                    return {"text": None, "thread": thread, "error": str(msg["error"])[:200]}
                new_thread = ((msg.get("result") or {}).get("thread") or {}).get("id") or thread

                from . import codex_exec
                turn_params = {"threadId": new_thread, "input": [{"type": "text", "text": prompt}],
                               "cwd": task_cwd, "sandboxPolicy": {
                                   "type": "readOnly" if sandbox == "read-only" else "workspaceWrite"},
                               "approvalPolicy": approval_policy}
                if bare_model:
                    turn_params["model"] = bare_model
                effort = codex_exec._codex_effort(reasoning)
                if effort:
                    turn_params["effort"] = effort
                turn_rid = self._next_id()
                turn_waiter = self._register_waiter(turn_rid, turn_thread_id=new_thread)
                try:
                    try:
                        self._write_rpc(p, {"jsonrpc": "2.0", "id": turn_rid, "method": "turn/start",
                                            "params": turn_params})
                    except (BrokenPipeError, OSError):
                        turn_waiter["dead"] = True
                        turn_waiter["msg"] = {"__dead__": True}
                        turn_waiter["event"].set()
                        turn_waiter["turn_event"].set()
                    got_response = turn_waiter["event"].wait(remaining_timeout())
                    turn_msg = turn_waiter["msg"] if got_response else None
                    if turn_msg and not turn_msg.get("__dead__") and not turn_msg.get("error"):
                        turn_waiter["turn_event"].wait(remaining_timeout())
                    completed_turn = turn_waiter["turn"]
                finally:
                    self._drop_waiter(turn_rid)
                    self._drop_turn_waiter(turn_waiter)
                if turn_waiter["dead"]:
                    self.shutdown()
                    if attempt == 2:
                        return {"text": None, "thread": new_thread,
                                "error": "codex app-server turn failed after restart"}
                    continue
                if turn_msg is not None and turn_msg.get("error"):
                    return {"text": None, "thread": new_thread, "error": str(turn_msg["error"])[:200]}
                if turn_msg is None or completed_turn is None:
                    return {"text": None, "thread": new_thread,
                            "error": f"codex warm call timeout ({CALL_TIMEOUT_S}s)"}
                status = completed_turn.get("status")
                if status != "completed":
                    error = completed_turn.get("error") or f"codex turn {status or 'failed'}"
                    return {"text": None, "thread": new_thread, "error": str(error)[:200], "tool_error": True}
                text = "".join(turn_waiter["texts"]).strip()
                return {"text": text or None, "thread": new_thread,
                        "error": None if text else "empty codex reply"}
        finally:
            self._schedule_idle_shutdown()


# ── ONE per-process daemon instance; the module API is the singleton's BOUND METHODS (assignment, not a second
# `def` — so the public names delegate without duplicating the class methods' definitions) ──
_DAEMON = _CodexDaemon()
run_warm = _DAEMON.run_warm            # the existing lane/callers' API (codex_exec.run_prompt → codex_daemon.run_warm)
ensure_running = _DAEMON.ensure_running
running = _DAEMON.running
shutdown = _DAEMON.shutdown

atexit.register(shutdown)
