"""WARM Codex lane over a persistent `codex app-server` — CONCURRENT (multiplexed JSON-RPC).

WHY. A one-shot `codex exec` COLD-STARTS every call (writable-workspace sandbox + loading all enabled plugins/MCP
servers) — MEASURED >75s, and intermittently hangs. `codex app-server` pays that setup ONCE at spawn; each request
is then a warm thread turn. This module holds ONE such server per process and reuses it.

TRANSPORT + LIFECYCLE is shared with the kimi ACP lane via `warm_stdio_daemon._WarmStdioJsonRpcDaemon` (the sole
background READER thread that demuxes responses by `id`, the write-lock-only-for-the-write concurrency, the
serialised spawn, the idle timer, and the dead-pipe fail-all). THIS module adds only what is codex-protocol-specific:
the `app-server` argv + initialize handshake, the two-phase `thread/start` → `turn/start` flow, and the notification
demux by (`threadId`, `turnId`) with the race-replay that a tiny turn can complete before its own turn/start
response is handled. CONTEXT: `thread/start` returns a thread id; passing it back through `thread/resume` CONTINUES
the conversation; omitting it (the default) starts a FRESH, context-isolated thread per call — that is what makes one
warm server a POOL of independent prompts rather than one growing conversation.
"""
import atexit
import os
import time

from . import config
from .warm_stdio_daemon import _SAFE_CWD, _WarmStdioJsonRpcDaemon

# MODULE-LEVEL constants (not only class attrs): run_warm reads CALL_TIMEOUT_S as a module global so a caller/test
# can monkeypatch `codex_daemon.CALL_TIMEOUT_S` and have the FALLBACK deadline change (the delegate timeout-kill
# guard relies on this). The class mirrors them for the shared base's handshake/idle use.
STARTUP_TIMEOUT_S = int(config._cfg_get("advisor", "lane_handshake_timeout_s", 25))  # one-time handshake budget,
#    config-gated (was 60): a down codex lane blocked 60s/retry → churn + metered fallback; 25s fails fast.
CALL_TIMEOUT_S = 180              # one warm turn, including setup + streamed completion (the fallback when none given)
IDLE_TIMEOUT_S = 600              # reclaim the persistent subprocess after ten minutes with no completed/new call


def _mcp_disable_flags():
    """Disable MCP orchestration globally for this headless app-server process.

    Codex 0.160.1's config explicitly gates configured servers on ``orchestrator.mcp.enabled``. Empty-table and
    per-entry overrides are not equivalent: config layers merge the former, while the latter can replace required
    transport fields and make app-server exit before the initialize handshake.
    """
    return ["-c", "orchestrator.mcp.enabled=false"]


class _CodexDaemon(_WarmStdioJsonRpcDaemon):
    """One warm `codex app-server` + a concurrent JSON-RPC client over it. The transport/lifecycle is inherited;
    this subclass owns the codex handshake, the thread/turn flow, and the (threadId, turnId) notification demux."""

    _LANE_LABEL = "codex"
    STARTUP_TIMEOUT_S = STARTUP_TIMEOUT_S     # mirror the module constants for the shared base (handshake / idle timer)
    CALL_TIMEOUT_S = CALL_TIMEOUT_S
    IDLE_TIMEOUT_S = IDLE_TIMEOUT_S

    def __init__(self):
        super().__init__()
        self._turn_waiters = {}                # (thread_id, turn_id) -> completion waiter
        self._pending_turn_notifications = {}  # notification can race ahead of the turn/start response

    # ── codex notification demux (by threadId/turnId), overriding the base no-op ──────────────────────────────
    def _drop_turn_waiter(self, waiter):
        key = (waiter.get("turn_thread_id"), waiter.get("turn_id"))
        with self._state_lock:
            if self._turn_waiters.get(key) is waiter:
                self._turn_waiters.pop(key, None)

    def _extra_waiters_to_fail(self):
        with self._state_lock:
            return list(self._turn_waiters.values())

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

    def _on_response(self, waiter, msg):
        """A turn/start response reveals the turn id; install correlation and replay notifications that won the race."""
        if not waiter.get("turn_thread_id"):
            return
        turn_id = ((msg.get("result") or {}).get("turn") or {}).get("id")
        if turn_id:
            self._correlate_turn_waiter(waiter, waiter["turn_thread_id"], turn_id)

    def _correlate_turn_waiter(self, waiter, thread_id, turn_id):
        """Install turn correlation and replay real notifications that won the response/notification race."""
        key = (thread_id, turn_id)
        with self._state_lock:
            waiter["turn_id"] = turn_id
            self._turn_waiters[key] = waiter
            pending = self._pending_turn_notifications.pop(key, [])
        for notification in pending:
            self._route_notification(notification)

    # ── codex spawn + handshake, overriding the base hooks ────────────────────────────────────────────────────
    def _spawn_cmd(self):
        from . import codex_exec
        exe = codex_exec._bin()
        if not exe:
            return None
        return [exe, "app-server"] + codex_exec._plugin_disable_flags() + _mcp_disable_flags()

    def _handshake(self, p):
        """`initialize` (multiplexed through the reader) then the fire-and-forget `initialized`."""
        rid = self._next_id()
        w = self._register_waiter(rid)
        try:
            self._write_rpc(p, {"jsonrpc": "2.0", "id": rid, "method": "initialize",
                           "params": {"clientInfo": {"name": "spendguard", "title": "spendguard",
                                                     "version": "1"}}})
            ok = (w["event"].wait(self.STARTUP_TIMEOUT_S) and bool(w["msg"])
                  and not w["msg"].get("__dead__") and not w["msg"].get("error"))
        finally:
            self._drop_waiter(rid)
        if not ok:
            return False
        self._write_rpc(p, {"jsonrpc": "2.0", "method": "initialized", "params": {}})   # OSError → base _spawn fails it
        return True

    def run_warm(self, prompt, model=None, thread=None, reasoning=None, sandbox="read-only", cwd=None,
                 timeout=None, recycle_on_timeout=False):
        """One delegation on the WARM server, CONCURRENCY-SAFE — many callers in flight, each waiting on its OWN
        response by id, holding no lock. A single call that TIMES OUT fails only ITSELF (the caller falls back to
        the metered API); only a genuinely dead pipe restarts the server, so one slow turn never sinks the others.

        `timeout` (seconds) overrides the per-call deadline when given — a long AGENTIC repo turn needs far more than
        a meta prompt's default; a caller that passed no timeout keeps the fixed CALL_TIMEOUT_S. `recycle_on_timeout`
        is for a SINGLE-TENANT caller (an agentic delegation, not the concurrent meta-fan): on a timeout the turn is
        abandoned but the app-server keeps running it, so the server can no longer be trusted idle — HARD-kill and
        recycle it (shutdown → respawn next call) so the next delegation gets a fresh server and fails fast instead of
        stalling behind a wedged turn. The default (False) preserves the fail-only-this-call behavior the meta-fan
        relies on (one slow turn must not tear down the server other callers are using)."""
        prompt = (prompt or "")
        if sandbox not in ("read-only", "workspace-write"):
            return {"text": None, "thread": thread, "error": f"unsupported codex sandbox {sandbox!r}"}
        self._cancel_idle_shutdown()
        try:
            for attempt in (1, 2):
                p = self.ensure_running()
                if p is None:
                    return {"text": None, "thread": None, "error": "codex app-server would not start"}
                eff_timeout = timeout if (timeout and timeout > 0) else CALL_TIMEOUT_S   # caller's deadline wins; the
                #   fallback reads the MODULE global so a monkeypatch of codex_daemon.CALL_TIMEOUT_S takes effect
                deadline = time.monotonic() + eff_timeout

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
                if msg is None:                                   # THIS call timed out; the pipe may still be alive
                    if recycle_on_timeout:                        # single-tenant agentic delegate: a wedged turn means
                        self.shutdown()                           # the server can't be trusted idle → HARD-kill + respawn
                    else:
                        config.warn_once("[spendguard] codex warm daemon: a warm call exceeded %ds — failing that ONE "
                                         "call to the metered API (the server stays up for the others)" % eff_timeout)
                    return {"text": None, "thread": thread, "error": f"codex warm call timeout ({eff_timeout:.0f}s)"}
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
                turn_waiter = self._register_waiter(turn_rid, turn_thread_id=new_thread, turn_id=None,
                                                    texts=[], turn=None)
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
                    if recycle_on_timeout:                        # the turn wedged (pipe alive, no completion) → recycle
                        self.shutdown()                           # so the NEXT delegation gets a fresh server, not a stall
                    return {"text": None, "thread": new_thread,
                            "error": f"codex warm call timeout ({eff_timeout:.0f}s)"}
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
