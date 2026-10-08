"""Shared machinery for a WARM, persistent CLI lane over a line-delimited JSON-RPC server on stdio.

WHY THIS EXISTS. A one-shot provider-CLI spawn (`codex exec`, `kimi -p`, …) COLD-STARTS every call — the CLI
reloads its plugins / MCP servers / agent base each time (MEASURED: codex >75s cold → ~5s warm). A persistent
server mode (`codex app-server`, `kimi acp`) pays that setup ONCE at spawn; each subsequent request is a warm turn.
`codex_daemon._CodexDaemon` proved the concurrent-multiplexing pattern; this module LIFTS that transport + lifecycle
out so a second lane (kimi ACP) reuses it rather than carrying a drifting copy of ~180 lines of race-handling.

WHAT IS SHARED (here) vs PROVIDER-SPECIFIC (subclass):
  • SHARED — the transport and the process lifecycle: one background READER thread that demuxes responses by JSON-RPC
    `id`; a caller locks only for the fast stdin WRITE then waits on its OWN event holding no lock (so N turns stay in
    flight over one warm server); serialised spawn with a re-check (two callers never create two servers); an idle
    timer that reclaims the subprocess; a dead-pipe path that wakes EVERY pending caller so none hangs to its timeout;
    `atexit`-safe teardown.
  • PROVIDER-SPECIFIC — the subclass overrides four hooks: `_spawn_cmd()` (the argv, incl. resolving its own CLI and
    its startup flags), `_handshake(p)` (the protocol's initialize exchange), `_route_notification(msg)` (how this
    protocol streams notifications), and its own `run_warm(...)` (the session/turn flow). The subclass also implements
    whatever extra notification-waiter registry its protocol needs, and declares them via `_extra_waiters_to_fail()`
    so the shared dead-pipe path wakes them too.

CONTAINMENT. The server is spawned in a FIXED, non-external bootstrap directory (`_SAFE_CWD`), with a symlink-RESOLVED
executable that is verified to be a real executable file, and with EVERY metered provider key stripped from its env
(`config.lane_plan_env`) — so neither the executable, the working directory, nor the credentials can be steered by
any caller's prompt content, and a lane subprocess can never silently become a metered API charge.

Names are unique+semantic (NAME_REGISTRY): `run_warm` across daemons is the adjudicated PROTOCOL entrypoint — one
uniform warm-lane contract, each daemon its own body — not a collision.
"""
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

# A FIXED, non-external bootstrap directory for the server process. Each call carries its own explicit task cwd where
# the protocol supports it; keeping the daemon SPAWN here means its startup directory can never be steered by content.
_SAFE_CWD = tempfile.gettempdir()


class _WarmStdioJsonRpcDaemon:
    """One warm line-delimited JSON-RPC server on stdio + a CONCURRENT client over it. All mutable state lives on the
    instance (guarded by the instance locks), so N callers run concurrent turns with no serialising round-trip lock.

    Subclass contract — override these (the base supplies safe defaults for the notification hooks):
      • ``_LANE_LABEL`` (class attr): the lane name used in operator warnings (e.g. "codex", "kimi").
      • ``STARTUP_TIMEOUT_S`` / ``CALL_TIMEOUT_S`` / ``IDLE_TIMEOUT_S`` (class attrs): the per-lane deadlines.
      • ``_spawn_cmd()`` → the full argv (``argv[0]`` the CLI path), or ``None`` when the CLI is unavailable.
      • ``_handshake(p)`` → ``True`` once the protocol's initialize exchange has completed on process ``p``.
      • ``_route_notification(msg)`` → consume one id-less server message (default: ignore).
      • ``_on_response(waiter, msg)`` → react to a correlated response before its waiter is woken (default: nothing).
      • ``_extra_waiters_to_fail()`` → any provider-specific waiter dicts the dead-pipe path must also wake.
      • ``run_warm(...)`` → the session/turn flow (not provided here; it composes the primitives below).
    """

    _LANE_LABEL = "warm"
    STARTUP_TIMEOUT_S = 60       # the ONE-TIME server handshake budget
    CALL_TIMEOUT_S = 180         # one warm turn, including setup + streamed completion
    IDLE_TIMEOUT_S = 600         # reclaim the persistent subprocess after this long with no completed/new call

    def __init__(self):
        self._state_lock = threading.Lock()    # guards _proc lifecycle + the _waiters registry
        self._spawn_lock = threading.Lock()    # serialises SPAWNS (a slow handshake must not run under _state_lock)
        self._write_lock = threading.Lock()    # serialises stdin WRITES only — never held while awaiting a response
        self._proc = None
        self._idle_timer = None
        self._last_activity = 0.0
        self._waiters = {}                      # rpc_id -> request waiter
        self._ids = itertools.count(1)          # itertools.count.__next__ is atomic under the GIL

    # ── id + idle-timer primitives ────────────────────────────────────────────────────────────────────────────
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

        timer = threading.Timer(self.IDLE_TIMEOUT_S, _shutdown_if_still_idle)
        timer.daemon = True
        with self._state_lock:
            self._idle_timer = timer
        timer.start()

    # ── waiter registry (responses correlated by JSON-RPC id) ─────────────────────────────────────────────────
    def _register_waiter(self, rid, **extra):
        """A pending request waiter: ``event`` fires when the response for ``rid`` arrives (``msg`` holds it).
        ``turn_event`` is a SECOND event some protocols use to await a streamed completion that follows the
        response; it is always present so the dead-pipe path can wake it uniformly. Subclasses stash any extra
        per-call fields (texts accumulator, correlation ids, …) via ``extra``."""
        w = {"event": threading.Event(), "msg": None, "turn_event": threading.Event(), "dead": False}
        w.update(extra)
        with self._state_lock:
            self._waiters[rid] = w
        return w

    def _drop_waiter(self, rid):
        with self._state_lock:
            self._waiters.pop(rid, None)

    def _extra_waiters_to_fail(self):
        """Provider-specific waiter dicts (beyond ``self._waiters``) the dead-pipe path must also wake. Default none."""
        return []

    def _fail_all_waiters(self):
        """A dead/closed pipe: wake EVERY pending caller with a dead marker so none hangs to its full timeout."""
        with self._state_lock:
            registered = list(self._waiters.values())
        pending = list({id(w): w for w in [*registered, *self._extra_waiters_to_fail()]}.values())
        for w in pending:
            w["dead"] = True
            if w.get("msg") is None:
                w["msg"] = {"__dead__": True}
            w["event"].set()
            if "turn_event" in w:
                w["turn_event"].set()

    # ── the one writer + the one reader ───────────────────────────────────────────────────────────────────────
    def _write_rpc(self, p, obj):
        """Write one JSON-RPC message under the write lock (held only for the write, never across a response wait)."""
        line = (json.dumps(obj) + "\n").encode("utf-8")
        with self._write_lock:
            p.stdin.write(line)
            p.stdin.flush()

    def _route_notification(self, msg):
        """Consume one server message that is NOT a response to our request — i.e. anything carrying a ``method``:
        a notification (``method``, no ``id``) OR an incoming request FROM the server (``method`` AND ``id``, e.g. an
        ACP permission request). Default: ignore. A subclass that may receive server-initiated requests must detect
        ``msg.get("id") is not None`` here and WRITE a reply, or the server can block waiting for one."""
        return None

    def _on_response(self, waiter, msg):
        """React to a correlated response just before its waiter is woken (e.g. install turn correlation). Default: no-op."""
        return None

    def _reader_loop(self, p):
        """SOLE stdout reader for ``p``: deliver RESPONSES (an ``id`` and NO ``method``) to their waiter by ``id``,
        and hand every ``method``-bearing message (a notification, or a server-initiated request that also has an
        ``id``) to ``_route_notification``. On EOF/error it fails ALL waiters so no caller is left hanging."""
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
                    if not isinstance(msg, dict):
                        continue
                    mid = msg.get("id")
                    # A `method` marks a message the server ORIGINATED — a notification (no id) or a request to us
                    # (id present). Neither is a response to our request, so neither goes to the waiter path; a
                    # response is an id with result/error and NO method. This split is behaviour-identical for a
                    # server that only sends method-without-id notifications + id-without-method responses (codex).
                    if msg.get("method") is not None:
                        self._route_notification(msg)
                        continue
                    if mid is None:
                        continue
                    with self._state_lock:
                        w = self._waiters.get(mid)
                        if w is not None:
                            w["msg"] = msg
                    if w is not None:
                        self._on_response(w, msg)          # subclass hook (e.g. codex turn correlation)
                        w["event"].set()
        finally:
            self._fail_all_waiters()

    # ── process lifecycle ─────────────────────────────────────────────────────────────────────────────────────
    @staticmethod
    def _terminate(p):
        try:
            p.terminate()
        except Exception:
            pass

    def _spawn(self):
        """Start the server, START THE READER, run the protocol handshake; return the live process or None (a startup
        failure the caller degrades on — the lane may degrade, the advisor must not break). Called only under
        _spawn_lock, so there is never a concurrent second spawn. CONTAINMENT: a symlink-RESOLVED, verified-executable
        argv[0] with a CONSTANT cwd and NO metered key in the child env — none influenced by caller input."""
        cmd = self._spawn_cmd()
        if not cmd:
            config.warn_once("[spendguard] %s warm daemon: the CLI is not found — the lane is unavailable this run "
                             "and callers fall back to the metered API" % self._LANE_LABEL)
            return None
        exe = os.path.realpath(cmd[0])
        if not (os.path.isfile(exe) and os.access(exe, os.X_OK)):
            config.warn_once("[spendguard] %s warm daemon: resolved CLI path %r is not an executable file — the lane "
                             "is unavailable, falling back to the metered API" % (self._LANE_LABEL, exe))
            return None
        cmd = [exe] + list(cmd[1:])
        try:
            p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                 bufsize=0, cwd=_SAFE_CWD, env=config.lane_plan_env())   # no metered key in the child
        except Exception:
            return None
        fl = fcntl.fcntl(p.stdout.fileno(), fcntl.F_GETFL)     # non-blocking stdout for the reader's os.read
        fcntl.fcntl(p.stdout.fileno(), fcntl.F_SETFL, fl | os.O_NONBLOCK)
        threading.Thread(target=self._reader_loop, args=(p,), daemon=True).start()   # the sole reader for THIS proc
        try:
            ok = bool(self._handshake(p))
        except (BrokenPipeError, OSError):
            ok = False
        if not ok:
            config.warn_once("[spendguard] %s warm daemon: the initialize handshake did not complete within %ds — "
                             "treating the server as unavailable this run (falling back to the metered API)"
                             % (self._LANE_LABEL, self.STARTUP_TIMEOUT_S))
            self._terminate(p)
            return None
        return p

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

    # ── a shared request/response primitive the subclass run_warm() composes ──────────────────────────────────
    def _send_request(self, p, method, params, remaining_fn, **waiter_extra):
        """Send one request and wait for its response by id, with the write guarded against a dead pipe. Returns
        ``(waiter, msg)`` where ``msg`` is the response dict, ``{"__dead__": True}`` if the pipe died under the write,
        or ``None`` on timeout. The caller owns dropping the waiter (so a protocol that then awaits a streamed
        completion on the SAME waiter can keep it registered). ``remaining_fn()`` returns the seconds still allowed."""
        rid = self._next_id()
        w = self._register_waiter(rid, **waiter_extra)
        w["_rid"] = rid
        try:
            self._write_rpc(p, {"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
        except (BrokenPipeError, OSError):
            w["dead"] = True
            w["msg"] = {"__dead__": True}
            w["event"].set()
            if "turn_event" in w:
                w["turn_event"].set()
        got = w["event"].wait(remaining_fn())
        return w, (w["msg"] if got else None)
