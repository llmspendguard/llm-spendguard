"""WARM Codex lane over a persistent `codex app-server` — CONCURRENT (multiplexed JSON-RPC).

WHY. A one-shot `codex exec` COLD-STARTS every call (writable-workspace sandbox + loading all enabled plugins/MCP
servers) — MEASURED >75s, and intermittently hangs. `codex app-server` pays that setup ONCE at spawn; each request
is then a warm thread turn. The host broker owns a configured pool of these reusable servers.

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
import contextvars
import os
import threading
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
_POOL_SLOT_KEY = "codex-daemon-workers"
_CODEX_AUTH_HTTP_STATUSES = (401, 403)
_CODEX_AUTH_ERROR_CODES = ("unauthorized", "authenticationError", "authentication_error")


def _codex_rpc_auth_failure(error):
    """Parse structured protocol error codes; never infer authentication from prose."""
    if not isinstance(error, dict):
        return False
    if (error.get("httpStatusCode") in _CODEX_AUTH_HTTP_STATUSES
            or error.get("code") in _CODEX_AUTH_ERROR_CODES
            or error.get("codexErrorInfo") in _CODEX_AUTH_ERROR_CODES):
        return True
    return any(_codex_rpc_auth_failure(value) for value in error.values() if isinstance(value, dict))


def _codex_pool_error_result(error, thread, **extra):
    return {"text": None, "thread": thread, "error": str(error),
            "provider_error": error,
            "auth_error": _codex_rpc_auth_failure(error), **extra}


def _reap_codex_orphans():
    """Reclaim codex app-servers orphaned by a dead host (the cross-session leak the user hit as warm-daemon
    saturation / thrashing). Structural + safe-by-construction: reaps ONLY servers carrying spendguard's own
    owner-marker whose recorded owner is dead — never a live pool's servers, never the user's own codex. Called at
    pool startup and on every health tick so it runs continuously, not just once. Best-effort: a reap failure must
    never break warming or the advisor."""
    try:
        from .warm_stdio_daemon import reap_orphaned_warm_servers
        report = reap_orphaned_warm_servers(lane=_CodexDaemon._LANE_LABEL)
        if report.get("reaped"):
            config.warn_once(f"[spendguard] reaped {len(report['reaped'])} orphaned codex app-server(s): "
                             f"{report['reaped']}")
        return report
    except Exception as exc:
        config.warn_once(f"[spendguard] codex orphan reap failed: {exc}")
        return None


def _mcp_disable_flags():
    """Disable MCP orchestration for this headless app-server process — the ONE definition lives in codex_exec (so the
    cold read-only `codex exec` path and this warm daemon share the setting to avoid orphaned MCP servers).
    MCP-tool use in delegation is available only on the cold workspace-write path; exec's --approve-for-me
    does not apply to this app-server. Codex 0.160.1 gates configured servers on ``orchestrator.mcp.enabled``
    key; the global gate is used, not per-entry overrides (those can break the initialize handshake)."""
    from . import codex_exec
    return codex_exec._mcp_disable_flags()


class _CodexDaemon(_WarmStdioJsonRpcDaemon):
    """One warm `codex app-server` + a concurrent JSON-RPC client over it. The transport/lifecycle is inherited;
    this subclass owns the codex handshake, the thread/turn flow, and the (threadId, turnId) notification demux."""

    _LANE_LABEL = "codex"
    STARTUP_TIMEOUT_S = STARTUP_TIMEOUT_S     # mirror the module constants for the shared base (handshake / idle timer)
    CALL_TIMEOUT_S = CALL_TIMEOUT_S
    STARTUP_TIMEOUT_S = STARTUP_TIMEOUT_S
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
                 timeout=None, recycle_on_timeout=None):
        """One delegation on the WARM server, CONCURRENCY-SAFE — many callers in flight, each waiting on its OWN
        response by id, holding no lock. A single call that TIMES OUT fails only ITSELF (the caller falls back to
        the metered API); only a genuinely dead pipe restarts the server, so one slow turn never sinks the others.

        This is the operative default-on Codex lane path: measured at ~5s warm. The >75s measurement applies only to
        a cold one-shot ``codex exec`` process, not to a request served here. See README.md's routing cheat-sheet.

        `timeout` (seconds) overrides the per-call deadline when given — a long AGENTIC repo turn needs far more than
        a meta prompt's default; a caller that passed no timeout keeps the fixed CALL_TIMEOUT_S. `recycle_on_timeout`
        is for a SINGLE-TENANT caller (an agentic delegation, not the concurrent meta-fan): on a timeout the turn is
        abandoned but the app-server keeps running it, so the server can no longer be trusted idle — HARD-kill and
        recycle it (shutdown → respawn next call) so the next delegation gets a fresh server and fails fast instead of
        stalling behind a wedged turn. The advisor/env default enables recycling for exclusive pool leases;
        callers sharing a raw daemon can explicitly opt out."""
        if recycle_on_timeout is None:
            recycle_on_timeout = config.codex_daemon_admission_settings()["recycle_on_timeout"]
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
                    return {"text": None, "thread": thread, "error": f"codex warm call timeout ({eff_timeout:.0f}s)",
                            "timed_out": True}
                if msg.get("error"):
                    return _codex_pool_error_result(msg["error"], thread)
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
                    return _codex_pool_error_result(turn_msg["error"], new_thread)
                if turn_msg is None or completed_turn is None:
                    if recycle_on_timeout:                        # the turn wedged (pipe alive, no completion) → recycle
                        self.shutdown()                           # so the NEXT delegation gets a fresh server, not a stall
                    return {"text": None, "thread": new_thread,
                            "error": f"codex warm call timeout ({eff_timeout:.0f}s)", "timed_out": True}
                status = completed_turn.get("status")
                if status != "completed":
                    error = completed_turn.get("error") or f"codex turn {status or 'failed'}"
                    return _codex_pool_error_result(error, new_thread, tool_error=True)
                text = "".join(turn_waiter["texts"]).strip()
                return {"text": text or None, "thread": new_thread,
                        "turn": turn_waiter.get("turn_id"), "process_pid": getattr(p, "pid", None),
                        "error": None if text else "empty codex reply"}
        finally:
            self._schedule_idle_shutdown()


# ── ONE host-broker facade per process.  Its owner proxies into the existing private-stdio daemon; clients attach
# over the Unix socket.  The module API remains the singleton's BOUND METHODS (assignment, not duplicate definitions). ──
class _CodexDaemonPool:
    """Exclusive worker leases behind the existing host broker and dispatch admission bucket.

    Workers retain sole ownership of spawn, polling and reap. The pool only leases instances and
    keeps the configured floor warm; no process lifecycle or semaphore implementation lives here.
    """

    CALL_TIMEOUT_S = CALL_TIMEOUT_S
    STARTUP_TIMEOUT_S = STARTUP_TIMEOUT_S

    def __init__(self, worker_factory, size=None):
        from .dispatch import _Bucket, effective_codex_concurrency
        settings = config.codex_daemon_pool_settings()
        self._size = settings["size"] if size is None else int(size)
        if self._size < 1:
            raise ValueError("codex daemon pool size must be positive")
        self._size = effective_codex_concurrency(self._size)
        self._workers = [worker_factory() for _ in range(self._size)]
        self._pool_worker_factory = worker_factory
        for index, worker in enumerate(self._workers):
            worker._keep_warm = True
            home = config.codex_daemon_worker_home(index) if settings["isolate_homes"] else None
            if home is not None:
                worker._environment_overrides = {"CODEX_HOME": str(home)}
        self._slots = _Bucket(_POOL_SLOT_KEY, self._size, rpm=0,
                              max_waiters=config.codex_daemon_admission_settings()["max_waiters"])
        self._pool_lock = threading.Condition()
        self._idle_workers = list(self._workers)
        self._pool_stopping = threading.Event()
        self._pool_monitor = None
        self._pool_recoveries = set()
        self._pool_degraded = {}

    def pool_ensure_running(self):
        # Before warming a fresh pool, reclaim any codex app-servers orphaned by a previously-dead host, so a new
        # pool never starts on top of a pile of leaked servers from earlier sessions.
        _reap_codex_orphans()
        # Warm concurrently; the inherited per-instance spawn lock also makes concurrent callers safe.
        results = [None] * self._size

        def warm_pool_child(index, worker):
            try:
                results[index] = worker.ensure_running()
            except Exception as exc:
                config.warn_once(f"[spendguard] codex pool startup: {exc}")

        starters = [threading.Thread(target=warm_pool_child, args=(index, worker), daemon=True)
                    for index, worker in enumerate(self._workers)]
        for starter in starters:
            starter.start()
        for starter in starters:
            starter.join()
        with self._pool_lock:
            if self._pool_monitor is None or not self._pool_monitor.is_alive():
                self._pool_stopping.clear()
                self._pool_monitor = threading.Thread(target=self._pool_maintain_warmth, daemon=True)
                self._pool_monitor.start()
        return next((result for result in results if result is not None), None)

    def _pool_maintain_warmth(self):
        interval = config.codex_daemon_pool_settings()["health_interval_s"]
        while not self._pool_stopping.wait(interval):
            # Continuously reclaim orphaned codex app-servers (a host that died WHILE this pool runs leaks its
            # servers too), so the leak can never accumulate between restarts — the active reaper the user asked for.
            _reap_codex_orphans()
            # Idle health work leases through the same bucket. Agentic leases have an independent
            # deadline in pool_run_warm, so even an invocation that never returns is reclaimed.
            from .dispatch import DispatchTimeout
            try:
                self._slots.acquire(0)
            except DispatchTimeout:
                continue
            worker = None
            transferred = False
            try:
                with self._pool_lock:
                    if self._idle_workers:
                        worker = self._idle_workers.pop(0)
                if worker is not None and not worker.running():
                    self._pool_start_recovery(worker)
                    transferred = True
            except Exception as exc:
                config.warn_once(f"[spendguard] codex pool health: {exc}")
            finally:
                with self._pool_lock:
                    if worker is not None and not transferred:
                        self._idle_workers.append(worker)
                        self._pool_lock.notify_all()
                if not transferred:
                    self._slots.release()

    def _pool_checkin_worker(self, worker):
        with self._pool_lock:
            self._idle_workers.append(worker)
            self._pool_lock.notify_all()
        self._slots.release()

    def _pool_start_recovery(self, worker, retire=False):
        """Transfer exactly one existing lease to the existing asynchronous recovery path."""
        recovery = threading.Thread(target=self._pool_recover_worker, args=(worker, retire), daemon=True)
        with self._pool_lock:
            self._pool_recoveries.add(recovery)
        try:
            recovery.start()
        except BaseException:
            with self._pool_lock:
                self._pool_recoveries.discard(recovery)
            raise

    def _pool_recover_worker(self, worker, retire=False):
        """Keep a failed child's lease until its own lifecycle has reaped and replaced it."""
        with self._pool_lock:
            slot_index = self._workers.index(worker)
        settings = config.codex_daemon_pool_settings()
        backoff = settings["respawn_backoff_s"]
        ceiling = max(backoff, settings["health_interval_s"])
        reaped = False
        try:
            while not self._pool_stopping.is_set():
                try:
                    if not reaped:
                        if retire:
                            worker._retired_from_pool = True
                        worker.shutdown()
                        reaped = True
                    if retire:
                        # A late return must not mutate the next lease's daemon/process registries.
                        replacement = self._pool_worker_factory()
                        replacement._keep_warm = True
                        replacement._environment_overrides = getattr(worker, "_environment_overrides", {})
                        with self._pool_lock:
                            self._workers[slot_index] = replacement
                        worker = replacement
                        retire = False
                    if worker.ensure_running() is not None:
                        with self._pool_lock:
                            self._pool_degraded.pop(slot_index, None)
                        break
                    failure = "worker spawn returned no process"
                except Exception as exc:
                    failure = str(exc)
                with self._pool_lock:
                    self._pool_degraded[slot_index] = failure
                config.warn_once(f"[spendguard] DEGRADED codex pool slot {slot_index}: {failure}; retrying respawn")
                if self._pool_stopping.wait(backoff):
                    break
                backoff = min(ceiling, backoff * config.CODEX_POOL_RESPAWN_BACKOFF_MULTIPLIER)
            if self._pool_stopping.is_set():
                worker.shutdown()
        finally:
            self._pool_checkin_worker(worker)
            with self._pool_lock:
                self._pool_recoveries.discard(threading.current_thread())

    def pool_run_warm(self, prompt, **kwargs):
        from .dispatch import DispatchTimeout
        kwargs = {"prompt": prompt, **kwargs}
        if kwargs.get("recycle_on_timeout") is None:
            kwargs["recycle_on_timeout"] = config.codex_daemon_admission_settings()["recycle_on_timeout"]
        deadline_s = kwargs.get("timeout") or self.CALL_TIMEOUT_S
        if deadline_s <= 0:
            deadline_s = self.CALL_TIMEOUT_S
        deadline = time.monotonic() + deadline_s
        result = None
        attempted_workers = set()
        for _ in range(self._size):
            result = None
            recycle = retry_auth = retire = False
            try:
                self._slots.acquire(max(0, deadline - time.monotonic()))
            except DispatchTimeout as exc:
                return {"text": None, "thread": kwargs.get("thread"), "error": str(exc)}
            with self._pool_lock:
                available = self._pool_lock.wait_for(
                    lambda: any(id(candidate) not in attempted_workers for candidate in self._idle_workers),
                    timeout=max(0, deadline - time.monotonic()))
                if not available:
                    self._slots.release()
                    return {"text": None, "thread": kwargs.get("thread"), "error": "codex pool checkout timeout"}
                worker = next(candidate for candidate in self._idle_workers
                              if id(candidate) not in attempted_workers)
                self._idle_workers.remove(worker)
                attempted_workers.add(id(worker))
            try:
                if self._size > 1 and time.monotonic() >= deadline:
                    return {"text": None, "thread": kwargs.get("thread"), "error": "codex pool checkout timeout"}
                call_kwargs = kwargs if self._size == 1 else {
                    **kwargs, "timeout": max(0, deadline - time.monotonic())}
                if kwargs.get("recycle_on_timeout"):
                    completed = threading.Event()
                    outcome = {}

                    def invoke_pool_lease(leased_worker=worker, options=call_kwargs,
                                          completion=completed, reply=outcome):
                        try:
                            reply["result"] = leased_worker.run_warm(**options)
                        except BaseException as exc:
                            reply["exception"] = exc
                        finally:
                            completion.set()

                    from .ledger_read import ledger_request_scope
                    with ledger_request_scope() as lease_reads:
                        lease_context = contextvars.copy_context()
                        threading.Thread(target=lease_context.run, args=(invoke_pool_lease,), daemon=True).start()
                        if not completed.wait(max(0, deadline - time.monotonic())):
                            # The worker lifecycle keeps its existing recycling
                            # policy; release any ledger work owned by this lease
                            # before returning a detached invocation to recovery.
                            lease_reads.cancel_ledger_request()
                            retire = True
                            result = {"text": None, "thread": kwargs.get("thread"),
                                      "error": "codex pool leased turn deadline expired", "timed_out": True}
                        elif "exception" in outcome:
                            raise outcome["exception"]
                        else:
                            result = outcome["result"]
                else:
                    result = worker.run_warm(**call_kwargs)
                recycle = bool(result.get("error"))
                retry_auth = bool(result.get("auth_error"))
            except BaseException:
                recycle = True
                raise
            finally:
                # Recovery retains this lease, but never delays the response or healthy-child failover.
                if recycle:
                    try:
                        self._pool_start_recovery(worker, retire)
                    except BaseException:
                        self._pool_checkin_worker(worker)
                        raise
                else:
                    self._pool_checkin_worker(worker)
            if not retry_auth:
                break
        if result is not None and ("auth_error" in result or "timed_out" in result):
            result = {key: value for key, value in result.items() if key not in ("auth_error", "timed_out")}
        return result

    def pool_running(self):
        return any(worker.running() for worker in self._workers)

    def pool_shutdown(self):
        self._pool_stopping.set()
        for worker in self._workers:
            worker.shutdown()

    ensure_running = pool_ensure_running
    run_warm = pool_run_warm
    running = pool_running
    shutdown = pool_shutdown


from .codex_daemon_broker import _CodexDaemonBroker

_LOCAL_DAEMON = _CodexDaemonPool(_CodexDaemon)
_DAEMON = _CodexDaemonBroker(_LOCAL_DAEMON)
run_warm = _DAEMON.broker_run_warm      # existing lane/callers' API (codex_exec.run_prompt → codex_daemon.run_warm)
ensure_running = _DAEMON.broker_ensure_running
running = _DAEMON.broker_running
shutdown = _DAEMON.broker_shutdown


def run_dedicated_codex_broker_owner():
    """Run the sole pool-building facade with this process's exact PID/create-time marker."""
    from .warm_stdio_daemon import _warm_owner_marker_env
    owner = _CodexDaemonBroker(_LOCAL_DAEMON, owner_marker=_warm_owner_marker_env(_CodexDaemon._LANE_LABEL))
    return owner.broker_owner_serve_forever()

atexit.register(shutdown)
