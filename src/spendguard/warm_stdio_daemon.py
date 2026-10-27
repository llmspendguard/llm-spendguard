"""Shared machinery for a WARM, persistent CLI lane over a line-delimited JSON-RPC server on stdio.

WHY THIS EXISTS. A one-shot provider-CLI spawn (`codex exec`, `kimi -p`, …) COLD-STARTS every call — the CLI
reloads its plugins / MCP servers / agent base each time (MEASURED: codex >75s cold → ~5s warm). A persistent
    server mode (`codex app-server`, `kimi acp`) pays that setup ONCE at spawn; each subsequent request is a warm turn.
    Thus ~5s is the operative measured Codex lane latency once the default-on daemon is enabled; >75s describes only
    the cold one-shot fallback, not normal warm lane routing. See README.md's routing cheat-sheet.
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
import errno
import fcntl
import itertools
import json
import math
import os
import select
import signal
import subprocess
import tempfile
import threading
import time

import psutil

from . import config

# A FIXED, non-external bootstrap directory for the server process. Each call carries its own explicit task cwd where
# the protocol supports it; keeping the daemon SPAWN here means its startup directory can never be steered by content.
_SAFE_CWD = tempfile.gettempdir()
WARM_EVIDENCE_CHUNK_DEFAULT_BYTES = 65536
WARM_STDERR_ARCHIVE_DIRECTORY_DEFAULT = "warm-transport-stderr"
WARM_STDERR_ARCHIVE_PREFIX = "warm-stderr-"
_WARM_REAP_TIMEOUT_S = 5
_WARM_GROUP_POLL_INTERVAL_S = 0.05
_WARM_GROUP_PROBE_SIGNAL = 0
_WARM_FORCE_KILL_SIGNAL = getattr(signal, "SIGKILL", None)

# ── warm-server OWNER MARKER + cross-session ORPHAN REAPER ────────────────────────────────────────────────────────
# A warm server is a child of whatever process built the pool (an mcp daemon, a `delegate`, a hook). If that owner
# dies by SIGKILL/crash — bypassing its atexit teardown — the server is ORPHANED: its stdio pipes to the dead owner
# are broken so it can serve no one, yet it keeps its RAM. Reaping is otherwise owned by the (now-dead) worker, so
# these leak across sessions and accumulate. The reaper closes that gap. It identifies spendguard's own servers
# STRUCTURALLY — by a marker spendguard stamps on the child's environment at spawn — NEVER by guessing from a
# command line (which would be a meaning-decision AND would risk killing the user's own codex/kimi processes). The
# owner is recorded as (pid, create_time): a recycle-proof identity, so a reused pid belonging to a NEW process is
# not mistaken for the dead owner still being alive.
WARM_OWNER_PID_ENV = "SPENDGUARD_WARM_OWNER_PID"
WARM_OWNER_BOOT_ENV = "SPENDGUARD_WARM_OWNER_BOOT"       # owner psutil create_time(), repr() of a float
WARM_OWNER_LANE_ENV = "SPENDGUARD_WARM_LANE"             # the lane label of the server (codex / kimi / …)
WARM_OWNER_BOOT_EPSILON_S = 0.05                         # create_time() float-formatting tolerance
WARM_OWNER_IDENTITY_ALIVE = "alive"
WARM_OWNER_IDENTITY_DEAD = "dead"
WARM_OWNER_IDENTITY_UNKNOWN = "unknown"


def _warm_owner_marker_env(lane):
    """The owner-identity env stamped onto every warm server spendguard spawns, so the reaper can later decide —
    structurally, from a fact spendguard itself wrote — whether this process is one of ours and whether its owner is
    still alive. Stamps (pid, create_time); if create_time is unreadable the reaper treats the owner as unknown and
    NEVER reaps, so a missing identity can only under-reap, never over-reap."""
    marker = {WARM_OWNER_LANE_ENV: str(lane), WARM_OWNER_PID_ENV: str(os.getpid())}
    try:
        marker[WARM_OWNER_BOOT_ENV] = repr(psutil.Process(os.getpid()).create_time())
    except Exception:
        pass
    return marker


def _warm_owner_identity_state(owner_pid, owner_boot_repr):
    """Classify a recorded owner identity without ever conflating an unverifiable owner with a dead one."""
    try:
        owner = psutil.Process(owner_pid)
    except psutil.NoSuchProcess:
        return WARM_OWNER_IDENTITY_DEAD                 # the owner pid is gone → provably dead
    except Exception:
        return WARM_OWNER_IDENTITY_UNKNOWN              # cannot query the pid → never reap
    if owner_boot_repr is None:
        return WARM_OWNER_IDENTITY_UNKNOWN              # pid exists, no identity to confirm recycling
    try:
        recorded_boot = float(owner_boot_repr)
    except (TypeError, ValueError):
        return WARM_OWNER_IDENTITY_UNKNOWN              # malformed identity → never reap
    try:
        recycled = abs(owner.create_time() - recorded_boot) >= WARM_OWNER_BOOT_EPSILON_S
        return WARM_OWNER_IDENTITY_DEAD if recycled else WARM_OWNER_IDENTITY_ALIVE
    except Exception:
        return WARM_OWNER_IDENTITY_UNKNOWN


def _warm_owner_provably_dead(owner_pid, owner_boot_repr):
    """True ONLY when the recorded recycle-proof owner identity is proven gone; unknown is never dead."""
    return _warm_owner_identity_state(owner_pid, owner_boot_repr) == WARM_OWNER_IDENTITY_DEAD


def _warm_reap_process_group(pid):
    """Signal a warm server's whole process GROUP (it is a session leader — start_new_session at spawn — so its pgid
    is its pid), SIGTERM then SIGKILL after a short grace. Returns True once no group member remains."""
    if not (hasattr(os, "killpg") and hasattr(os, "getpgid")):
        try:
            os.kill(pid, _WARM_FORCE_KILL_SIGNAL or signal.SIGTERM)
        except ProcessLookupError:
            return True
        except Exception:
            return False
        return True
    deadline = time.monotonic() + _WARM_REAP_TIMEOUT_S
    for sig in (signal.SIGTERM, _WARM_FORCE_KILL_SIGNAL or signal.SIGTERM):
        try:
            os.killpg(pid, sig)
        except ProcessLookupError:
            return True
        except Exception:
            return False
        while time.monotonic() < deadline:
            try:
                os.killpg(pid, _WARM_GROUP_PROBE_SIGNAL)     # signal 0 = liveness probe, kills nothing
            except ProcessLookupError:
                return True
            except Exception:
                break
            time.sleep(_WARM_GROUP_POLL_INTERVAL_S)
    try:
        os.killpg(pid, _WARM_GROUP_PROBE_SIGNAL)
        return False
    except ProcessLookupError:
        return True
    except Exception:
        return False


def reap_orphaned_warm_servers(lane=None, execute=True):
    """Reclaim warm-lane servers spendguard spawned whose spawning OWNER is no longer alive (the cross-session leak).

    Selection is STRUCTURAL: a candidate is only a process carrying spendguard's own owner-marker env (stamped at
    spawn), and it is reaped ONLY when its recorded owner (pid, create_time) is not a currently-living process with
    that identity. A live pool's servers (owner alive) and the user's own codex/kimi processes (no marker) are never
    touched. Each orphan is a session leader, so its whole process group is signalled.

    lane: restrict to one lane label (e.g. "codex"); None reaps every warm lane.
    execute: False ⇒ dry run — return the orphans that WOULD be reaped without signalling any.
    Returns {"scanned", "orphans", "reaped", "cross_uid_skipped", "unreadable", "errors", "marked"}. ``marked``
    exposes the lane and
    owner identity facts from THIS SAME authoritative scan for operator reporting; it does not add another selector.
    `cross_uid_skipped` counts permission-denied environments, which belong to another uid and therefore cannot be
    spendguard warm servers. `unreadable` names every OTHER environment read failure — a VISIBLE same-uid gap (a
    possible orphan we could not classify), never silently dropped: "cannot tell" is not "clean".
    """
    report = {"scanned": 0, "orphans": [], "reaped": [], "cross_uid_skipped": 0, "unreadable": [],
              "errors": [], "marked": []}
    for proc in psutil.process_iter(["pid"]):
        try:
            environ = proc.environ()
        except (psutil.NoSuchProcess, psutil.ZombieProcess):
            continue                                     # the process is genuinely gone — nothing to reap or report
        except Exception as exc:
            # Permission denial proves this is another uid's process: spendguard's children share this uid and are
            # readable. Count that non-candidate compactly; preserve every other read failure as a genuine gap.
            if isinstance(exc, psutil.AccessDenied) or (
                    isinstance(exc, OSError) and exc.errno in (errno.EACCES, errno.EPERM)):
                report["cross_uid_skipped"] += 1
                continue
            report["unreadable"].append(f"{getattr(proc, 'pid', '?')}: {type(exc).__name__}")
            continue
        owner_pid_raw = environ.get(WARM_OWNER_PID_ENV)
        if owner_pid_raw is None:
            continue                                     # not a spendguard-spawned warm server
        if lane is not None and environ.get(WARM_OWNER_LANE_ENV) != lane:
            continue
        report["scanned"] += 1
        try:
            owner_pid = int(owner_pid_raw)
        except (TypeError, ValueError):
            report["unreadable"].append(f"{proc.pid}: unparseable owner pid {owner_pid_raw!r}")
            continue                                     # owner id corrupt → owner unknown → never reap
        owner_state = _warm_owner_identity_state(owner_pid, environ.get(WARM_OWNER_BOOT_ENV))
        marked_server = {"pid": proc.pid, "lane": environ.get(WARM_OWNER_LANE_ENV), "owner_pid": owner_pid,
                         "owner_state": owner_state, "orphan": owner_state == WARM_OWNER_IDENTITY_DEAD}
        report["marked"].append(marked_server)
        if owner_state != WARM_OWNER_IDENTITY_DEAD:
            continue                                     # owner alive or unverifiable → server may be in use; never reap
        report["orphans"].append(proc.pid)
        if not execute:
            continue
        if _warm_reap_process_group(proc.pid):
            report["reaped"].append(proc.pid)
        else:
            report["errors"].append(f"reap({proc.pid}): group still alive after SIGKILL grace")
    return report


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
    STARTUP_TIMEOUT_S = int(config._cfg_get("advisor", "lane_handshake_timeout_s", 25))  # one-time handshake budget —
    #    config-gated (was a hard 60s): a DOWN lane blocked a pool thread for 60s per retry before falling back to
    #    metered, so a drain churned on an unavailable lane. 25s fails an unreachable lane ~2.4x faster; a live lane's
    #    cold handshake is well under it. Raise advisor.lane_handshake_timeout_s if a slow cold start needs more.
    CALL_TIMEOUT_S = 180         # one warm turn, including setup + streamed completion
    IDLE_TIMEOUT_S = 600         # reclaim the persistent subprocess after this long with no completed/new call

    def __init__(self):
        self._state_lock = threading.Lock()    # guards _proc lifecycle + the _waiters registry
        self._spawn_lock = threading.Lock()    # serialises SPAWNS (a slow handshake must not run under _state_lock)
        self._write_lock = threading.Lock()    # serialises stdin WRITES only — never held while awaiting a response
        self._proc = None
        self._retired_from_pool = False
        self._idle_timer = None
        self._last_activity = 0.0
        self._waiters = {}                      # rpc_id -> request waiter
        self._ids = itertools.count(1)          # itertools.count.__next__ is atomic under the GIL
        self._keep_warm = False
        self._environment_overrides = {}
        self._warm_evidence_scopes = {}

    def _enter_warm_evidence_scope(self):
        from . import execution_journal
        warm_execution_id = execution_journal.current_durable_execution_id()
        if warm_execution_id:
            self._warm_evidence_read_size()
            with self._state_lock:
                warm_capture = self._warm_evidence_scopes.setdefault(
                    warm_execution_id, {"users": 0, "error": None})
                warm_capture["users"] += 1
            if self._proc is not None:
                self._reference_warm_stderr_archive(self._proc, (warm_execution_id,))
        return warm_execution_id

    def _leave_warm_evidence_scope(self, warm_execution_id):
        if warm_execution_id:
            with self._state_lock:
                warm_capture = self._warm_evidence_scopes[warm_execution_id]
                warm_capture["users"] -= 1
                if not warm_capture["users"]:
                    del self._warm_evidence_scopes[warm_execution_id]
            if warm_capture["error"] is not None:
                raise warm_capture["error"]

    def _capture_warm_transport_bytes(self, *, stdout=None, stderr=None, exit_code=None):
        from . import execution_journal
        with self._state_lock:
            warm_capture_ids = tuple(self._warm_evidence_scopes)
        for warm_capture_id in warm_capture_ids:
            try:
                execution_journal.append_durable_execution_evidence(
                    warm_capture_id, stdout=stdout, stderr=stderr)
                if exit_code is not None:
                    execution_journal.persist_durable_execution_payload(warm_capture_id, exit_code=exit_code)
            except execution_journal.DurableExecutionPersistenceError as warm_capture_error:
                with self._state_lock:
                    warm_failed_scope = self._warm_evidence_scopes.get(warm_capture_id)
                    if warm_failed_scope is not None:
                        warm_failed_scope["error"] = warm_capture_error
                self._fail_all_waiters()

    @staticmethod
    def _warm_evidence_read_size():
        warm_read_size = int(os.getenv("SPENDGUARD_WARM_EVIDENCE_CHUNK_BYTES") or config._cfg_get(
            "delegate", "warm_evidence_chunk_bytes", WARM_EVIDENCE_CHUNK_DEFAULT_BYTES))
        if warm_read_size <= 0:
            raise ValueError("warm evidence chunk bytes must be positive")
        return warm_read_size

    def _reference_warm_stderr_archive(self, process, execution_ids=None):
        from . import execution_journal
        warm_archive_path = getattr(process, "_spendguard_stderr_archive", getattr(process, "name", None))
        if not isinstance(warm_archive_path, (str, os.PathLike)):
            return
        if execution_ids is None:
            with self._state_lock:
                execution_ids = tuple(self._warm_evidence_scopes)
        for warm_archive_execution in execution_ids:
            if warm_archive_execution is None:
                continue
            execution_journal.reference_durable_execution_stderr(warm_archive_execution, warm_archive_path)

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
        if self._keep_warm:
            return
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
        """Bound lock acquisition and partial writes to the unbuffered child pipe."""
        budget = float(config._cfg_get("advisor", "lane_write_timeout_s", self.STARTUP_TIMEOUT_S))
        if not math.isfinite(budget) or budget <= 0:
            raise ValueError("advisor.lane_write_timeout_s must be positive and finite")
        write_deadline = time.monotonic() + budget
        line = (json.dumps(obj) + "\n").encode("utf-8")
        if not self._write_lock.acquire(timeout=budget):
            raise TimeoutError("warm stdin write lock deadline expired")
        try:
            descriptor = p.stdin.fileno()
            os.set_blocking(descriptor, False)
            pending = memoryview(line)
            while pending:
                remaining = write_deadline - time.monotonic()
                if remaining <= 0 or not select.select([], [descriptor], [], remaining)[1]:
                    raise TimeoutError("warm stdin write deadline expired")
                try:
                    written = os.write(descriptor, pending)
                except (BlockingIOError, InterruptedError):
                    continue
                if not written:
                    raise BrokenPipeError("warm stdin closed during write")
                pending = pending[written:]
        finally:
            self._write_lock.release()

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
                try:
                    r, _, _ = select.select([p.stdout], [], [], 0.5)
                except (OSError, ValueError):
                    break
                if not r:
                    continue
                try:
                    chunk = os.read(p.stdout.fileno(), self._warm_evidence_read_size())
                except (BlockingIOError, InterruptedError):
                    continue
                except (OSError, ValueError):
                    break
                if chunk == b"":                           # EOF — server closed the pipe
                    break
                self._capture_warm_transport_bytes(stdout=chunk)
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
            self._capture_warm_transport_bytes(exit_code=p.poll())
            self._fail_all_waiters()

    # ── process lifecycle ─────────────────────────────────────────────────────────────────────────────────────
    @staticmethod
    def _warm_signal_owned_group(p, group_id, termination_signal):
        try:
            if group_id is not None:
                os.killpg(group_id, termination_signal)
            elif termination_signal == signal.SIGTERM:
                p.terminate()
            else:
                p.kill()
        except ProcessLookupError:
            return
        except Exception as exc:
            config.warn_once(f"[spendguard] warm daemon: signal {termination_signal} "
                             f"for process {p.pid} failed: {exc}")

    @staticmethod
    def _warm_owned_group_alive(group_id):
        try:
            os.killpg(group_id, _WARM_GROUP_PROBE_SIGNAL)
            return True
        except ProcessLookupError:
            return False
        except Exception as exc:
            config.warn_once(f"[spendguard] warm daemon: checking process group {group_id} failed: {exc}")
            return True  # A failed probe is not proof that descendants are gone.

    @classmethod
    def _terminate(cls, p):
        """Reap our session leader and terminate surviving descendants within the existing grace budget."""
        group_id = None
        if hasattr(os, "killpg") and hasattr(os, "getpgid"):
            # Recorded at spawn: a session leader's pgid is its pid. Keep it even after the
            # leader exits/reaps, when getpgid(pid) can no longer find surviving descendants.
            group_id = getattr(p, "_spendguard_process_group", None)
            if group_id is not None:
                try:
                    observed_group = os.getpgid(p.pid)
                    if observed_group != group_id:
                        config.warn_once(f"[spendguard] warm daemon: process {p.pid} moved from owned group "
                                         f"{group_id} to {observed_group}; retaining the owned group")
                except ProcessLookupError:
                    group_id = p._spendguard_process_group
                except Exception as exc:
                    config.warn_once(f"[spendguard] warm daemon: resolving group for {p.pid} failed: {exc}")
        deadline = time.monotonic() + _WARM_REAP_TIMEOUT_S
        cls._warm_signal_owned_group(p, group_id, signal.SIGTERM)
        escalate = False
        try:
            p.wait(timeout=_WARM_REAP_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            escalate = True
        except Exception as exc:
            config.warn_once(f"[spendguard] warm daemon: waiting for process {p.pid} failed: {exc}")
            escalate = True
        if group_id is not None:
            while cls._warm_owned_group_alive(group_id):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    escalate = True
                    break
                time.sleep(min(_WARM_GROUP_POLL_INTERVAL_S, remaining))
        if escalate:
            cls._warm_signal_owned_group(p, group_id, _WARM_FORCE_KILL_SIGNAL)
            try:
                p.wait(timeout=_WARM_REAP_TIMEOUT_S)
            except Exception as exc:
                config.warn_once(f"[spendguard] warm daemon: reaping process {p.pid} after kill failed: {exc}")

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
        launcher = cmd[0]                                      # the RESOLVED CLI as found (often a symlink, e.g. the
        #   nvm `codex` whose sibling is node). lane_exec_env must prepend THIS dir — realpath() would resolve the
        #   symlink to .../lib/node_modules/@openai/codex/bin/codex.js, whose dir has NO node, so the `env node`
        #   shebang then fails under a daemon's minimal PATH and the app-server dies before the handshake.
        exe = os.path.realpath(launcher)
        if not (os.path.isfile(exe) and os.access(exe, os.X_OK)):
            config.warn_once("[spendguard] %s warm daemon: resolved CLI path %r is not an executable file — the lane "
                             "is unavailable, falling back to the metered API" % (self._LANE_LABEL, exe))
            return None
        cmd = [exe] + list(cmd[1:])
        from . import execution_journal
        warm_archive_directory = execution_journal.durable_execution_store_root() / str(config._cfg_get(
            "delegate", "warm_stderr_archive_directory", WARM_STDERR_ARCHIVE_DIRECTORY_DEFAULT))
        warm_archive_directory.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(prefix=WARM_STDERR_ARCHIVE_PREFIX, dir=warm_archive_directory,
                                         delete=False) as warm_stderr_archive:
            self._reference_warm_stderr_archive(warm_stderr_archive)
            try:
                p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=warm_stderr_archive,
                                 bufsize=0, cwd=_SAFE_CWD,
                                 start_new_session=hasattr(os, "killpg") and hasattr(os, "getpgid"),
                                 env={**config.lane_exec_env(launcher), **self._environment_overrides,
                                      **_warm_owner_marker_env(self._LANE_LABEL)})   # owner identity last: authoritative
            except Exception:
                return None
        p._spendguard_stderr_archive = warm_stderr_archive.name
        if hasattr(os, "killpg") and hasattr(os, "getpgid"):
            p._spendguard_process_group = p.pid
        try:
            fl = fcntl.fcntl(p.stdout.fileno(), fcntl.F_GETFL)     # non-blocking stdout for the reader's os.read
            fcntl.fcntl(p.stdout.fileno(), fcntl.F_SETFL, fl | os.O_NONBLOCK)
            threading.Thread(target=self._reader_loop, args=(p,), daemon=True).start()   # the sole reader for THIS proc
            try:
                ok = bool(self._handshake(p))
            except (BrokenPipeError, OSError):
                ok = False
        except BaseException:
            self._terminate(p)
            raise
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
            if self._retired_from_pool:
                return None
            if self._proc is not None and self._proc.poll() is None:
                return self._proc
        with self._spawn_lock:                        # only one spawner at a time
            with self._state_lock:                    # re-check: another thread may have spawned while we waited
                if self._retired_from_pool:
                    return None
                if self._proc is not None and self._proc.poll() is None:
                    return self._proc
                previous, self._proc = self._proc, None
            if previous is not None:
                self._terminate(previous)  # A dead session leader can still have live descendants.
            p = self._spawn()                         # slow handshake — outside _state_lock, inside _spawn_lock
            with self._state_lock:
                retired = self._retired_from_pool
                self._proc = p
            if retired:
                # Retirement may race the slow handshake; use the normal kill/reap path.
                self.shutdown()
                return None
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
