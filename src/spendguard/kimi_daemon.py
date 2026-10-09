"""WARM Kimi Code lane over a persistent `kimi acp` server — CONCURRENT (multiplexed JSON-RPC / ACP over stdio).

WHY. A one-shot `kimi -p` COLD-STARTS every call (the Kimi Code CLI reloads its agent base + config each spawn).
`kimi acp` runs kimi-code as an Agent Client Protocol (ACP) server over stdio: the setup is paid ONCE at spawn, and
each prompt is then a warm turn. CRITICALLY, ACP `session/new` begins a BRAND-NEW, context-isolated session (spec:
"a new session starts clean; only session/load restores prior history"), so one warm server is a POOL of INDEPENDENT
prompts — not one growing conversation that would contaminate independent comprehension prompts and grow in cost.
This is the exact property that makes codex's `thread/start` a warm pool; `session/new` is its ACP analogue.

TRANSPORT + LIFECYCLE is shared with the codex lane via `warm_stdio_daemon._WarmStdioJsonRpcDaemon` (the sole reader
thread demuxing responses by `id`, write-lock-only concurrency, serialised spawn, idle timer, dead-pipe fail-all).
THIS module adds only what is ACP-protocol-specific: the `acp` argv + the ACP `initialize` handshake (protocolVersion
1, NO separate `initialized` notification — unlike LSP), and the `session/new` → `session/prompt` flow whose streamed
`session/update` / `agent_message_chunk` notifications are concatenated into the final answer while the terminating
`session/prompt` response carries `stopReason`.

MODEL FIDELITY. `kimi acp` has no model flag — it answers on the plan's `default_model`. The caller (`kimi_exec`)
only routes to this warm path when the requested model resolves to that default (or is unpinned); a specific other
model falls through to the cold `-p -m <alias>` path. So the recorded model always matches the model that ran.

A refusal or cancellation is NOT a protocol error in ACP — it returns a normal result with `stopReason`
`refusal`/`cancelled`; we surface it as a HARD (non-retryable) tool_error, exactly as codex treats a non-completed
turn, so the caller falls back to the metered API and backs off the lane rather than recording a refusal as content.
"""
import atexit

from . import config
from .warm_stdio_daemon import _SAFE_CWD, _WarmStdioJsonRpcDaemon

ACP_PROTOCOL_VERSION = 1       # ACP protocolVersion is an INTEGER; 1 is current. A mismatch degrades to the cold path.
# ACP stopReason values that carry a usable completed answer vs. a hard user-visible refusal (NOT a protocol error).
_STOP_OK = ("end_turn", "max_tokens", "max_turn_requests")
_STOP_HARD = ("refusal", "cancelled")


class _KimiAcpDaemon(_WarmStdioJsonRpcDaemon):
    """One warm `kimi acp` ACP server + a concurrent client over it. Transport/lifecycle is inherited; this subclass
    owns the ACP handshake, the session/new → session/prompt flow, and the session/update demux by `sessionId`."""

    _LANE_LABEL = "kimi"
    STARTUP_TIMEOUT_S = int(config._cfg_get("advisor", "lane_handshake_timeout_s", 25))  # one-time ACP init budget,
    #    config-gated (was 60): a down kimi lane blocked 60s/retry → churn; 25s fails fast (advisor.lane_handshake_timeout_s).
    CALL_TIMEOUT_S = 300             # one warm turn (matches the cold kimi_exec.TIMEOUT_S ceiling for meta prompts)
    IDLE_TIMEOUT_S = 600             # reclaim the persistent subprocess after ten minutes with no completed/new call

    def __init__(self):
        super().__init__()
        self._session_waiters = {}             # sessionId -> the in-flight prompt waiter (streamed text accumulator)

    # ── ACP notification / server-request demux, overriding the base no-op ────────────────────────────────────
    def _extra_waiters_to_fail(self):
        with self._state_lock:
            return list(self._session_waiters.values())

    def _drop_session_waiter(self, session_id):
        with self._state_lock:
            self._session_waiters.pop(session_id, None)

    def _route_notification(self, msg):
        """Handle a server-ORIGINATED message: an ACP `session/update` notification (stream agent text), or a
        server→client REQUEST (has an `id`) which we must answer so the agent never blocks. A self-contained meta
        prompt needs no tools, so any such request (e.g. a permission/fs/terminal ask) is DECLINED with a JSON-RPC
        error — we advertised no client capabilities — rather than left unanswered."""
        rid = msg.get("id")
        if rid is not None:
            p = self._proc
            if p is not None:
                try:
                    self._write_rpc(p, {"jsonrpc": "2.0", "id": rid, "error": {
                        "code": -32601, "message": "spendguard ACP client declines server-initiated requests"}})
                except (BrokenPipeError, OSError):
                    pass
            return
        if msg.get("method") != "session/update":
            return
        params = msg.get("params") or {}
        update = params.get("update") or {}
        if update.get("sessionUpdate") != "agent_message_chunk":   # agent_thought_chunk (reasoning) etc. are NOT the answer
            return
        content = update.get("content") or {}                      # agent_message_chunk carries ONE content block, not a list
        if content.get("type") == "text" and isinstance(content.get("text"), str):
            with self._state_lock:
                w = self._session_waiters.get(params.get("sessionId"))
                if w is not None:
                    w["texts"].append(content["text"])

    # ── ACP spawn + handshake, overriding the base hooks ──────────────────────────────────────────────────────
    def _spawn_cmd(self):
        from . import kimi_exec
        exe = kimi_exec._bin()
        if not exe:
            return None
        return [exe, "acp"]

    def _handshake(self, p):
        """ACP `initialize`: advertise NO client capabilities (prompt-mode meta work needs no fs/terminal). ACP has
        NO separate `initialized` notification, so `session/new` follows directly on each call. Degrade (return
        False) on a dead/timeout/error response — a startup failure must never break the advisor, only this lane."""
        rid = self._next_id()
        w = self._register_waiter(rid)
        try:
            self._write_rpc(p, {"jsonrpc": "2.0", "id": rid, "method": "initialize", "params": {
                "protocolVersion": ACP_PROTOCOL_VERSION,
                "clientCapabilities": {"fs": {"readTextFile": False, "writeTextFile": False}, "terminal": False},
                "clientInfo": {"name": "spendguard", "version": "1"}}})
            ok = (w["event"].wait(self.STARTUP_TIMEOUT_S) and bool(w["msg"])
                  and not w["msg"].get("__dead__") and not w["msg"].get("error"))
        finally:
            self._drop_waiter(rid)
        return bool(ok)

    def run_warm(self, prompt, model=None, reasoning=None, timeout=None, cwd=None, recycle_on_timeout=False):
        """One delegation on the WARM ACP server, CONCURRENCY-SAFE. Each call opens a FRESH `session/new` (isolated
        context), sends one `session/prompt`, concatenates the streamed `agent_message_chunk` text, and returns on the
        `stopReason` the prompt response carries. → {"text", "error"} (plus "tool_error" on a hard refusal). A call
        that TIMES OUT fails only ITSELF (the server stays up for the others); only a genuinely dead pipe restarts the
        server, so one slow turn never sinks the others. `model`/`reasoning` are accepted for a uniform lane contract
        but NOT used to select — `kimi acp` answers on the plan default, and the caller only routes here when that is
        the requested model (see module docstring). `recycle_on_timeout` HARD-kills the server on a timeout for a
        single-tenant caller, matching the codex lane."""
        import os
        import time

        prompt = (prompt or "")
        self._cancel_idle_shutdown()
        try:
            for attempt in (1, 2):
                p = self.ensure_running()
                if p is None:
                    return {"text": None, "error": "kimi acp server would not start"}
                eff_timeout = timeout if (timeout and timeout > 0) else self.CALL_TIMEOUT_S
                deadline = time.monotonic() + eff_timeout

                def remaining_timeout(call_deadline=deadline):
                    return max(0.0, call_deadline - time.monotonic())

                task_cwd = os.path.abspath(cwd or _SAFE_CWD)

                # 1) session/new — a brand-new, context-isolated session (the POOL property).
                sw, new_msg = self._send_request(p, "session/new",
                                                 {"cwd": task_cwd, "mcpServers": []}, remaining_timeout)
                new_rid = sw["_rid"]
                self._drop_waiter(new_rid)
                if new_msg is not None and new_msg.get("__dead__"):
                    self.shutdown()
                    if attempt == 2:
                        config.warn_once("[spendguard] kimi warm daemon: session/new still failed after a restart — "
                                         "the lane is degrading to the metered API this run (not the advisor breaking)")
                        return {"text": None, "error": "kimi acp session/new failed after restart"}
                    continue
                if new_msg is None:                       # timed out before the session even opened
                    if recycle_on_timeout:
                        self.shutdown()
                    return {"text": None, "error": f"kimi warm session/new timeout ({eff_timeout:.0f}s)"}
                if new_msg.get("error"):
                    return {"text": None, "error": str(new_msg["error"])[:200]}
                session_id = ((new_msg.get("result") or {}).get("sessionId"))
                if not session_id:
                    return {"text": None, "error": "kimi acp session/new returned no sessionId"}

                # 2) session/prompt — register the sessionId→waiter mapping BEFORE the write so no streamed chunk is
                #    missed (no chunk can arrive for this session until the prompt is sent, but order it this way so
                #    the accumulator is always installed first). The TERMINATING signal is this request's RESPONSE
                #    (result.stopReason), not a notification.
                prompt_rid = self._next_id()
                pw = self._register_waiter(prompt_rid, texts=[], session_id=session_id)
                with self._state_lock:
                    self._session_waiters[session_id] = pw
                try:
                    try:
                        self._write_rpc(p, {"jsonrpc": "2.0", "id": prompt_rid, "method": "session/prompt", "params": {
                            "sessionId": session_id, "prompt": [{"type": "text", "text": prompt}]}})
                    except (BrokenPipeError, OSError):
                        pw["dead"] = True
                        pw["msg"] = {"__dead__": True}
                        pw["event"].set()
                    got = pw["event"].wait(remaining_timeout())
                    prompt_msg = pw["msg"] if got else None
                    text = "".join(pw["texts"]).strip()
                finally:
                    self._drop_waiter(prompt_rid)
                    self._drop_session_waiter(session_id)
                if prompt_msg is not None and prompt_msg.get("__dead__"):
                    self.shutdown()
                    if attempt == 2:
                        return {"text": None, "error": "kimi acp session/prompt failed after restart"}
                    continue
                if prompt_msg is None:                     # this call timed out; the pipe may still be alive
                    if recycle_on_timeout:
                        self.shutdown()
                    else:
                        config.warn_once("[spendguard] kimi warm daemon: a warm call exceeded %ds — failing that ONE "
                                         "call to the metered API (the server stays up for the others)" % eff_timeout)
                    return {"text": None, "error": f"kimi warm call timeout ({eff_timeout:.0f}s)"}
                if prompt_msg.get("error"):
                    return {"text": None, "error": str(prompt_msg["error"])[:200]}
                stop = ((prompt_msg.get("result") or {}).get("stopReason"))
                if stop in _STOP_HARD:                     # a user-visible refusal/cancel — HARD, non-retryable
                    return {"text": None, "error": f"kimi acp stopReason {stop}", "tool_error": True}
                if stop is not None and stop not in _STOP_OK:
                    return {"text": None, "error": f"kimi acp unexpected stopReason {stop}", "tool_error": True}
                return {"text": text or None, "error": None if text else "empty kimi reply"}
        finally:
            self._schedule_idle_shutdown()


# ── ONE per-process daemon instance; the module API is the singleton's BOUND METHODS (assignment, not a second
# `def` — so the public names delegate without duplicating the class methods' definitions) ──
_DAEMON = _KimiAcpDaemon()
run_warm = _DAEMON.run_warm            # the kimi lane's warm API (kimi_exec.run_prompt → kimi_daemon.run_warm)
ensure_running = _DAEMON.ensure_running
running = _DAEMON.running
shutdown = _DAEMON.shutdown

atexit.register(shutdown)
