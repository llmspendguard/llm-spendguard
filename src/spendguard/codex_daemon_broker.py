"""Host-wide Unix-socket broker for the warm Codex daemon.

The app-server itself deliberately remains the existing private-stdio daemon.  The process holding
``config.single_instance_lock("codex-daemon")`` owns those pipes and exposes only ``run_warm`` over a framed local
socket; every other process is a client.  Lock ownership, not a pid file or a signal, is the sole authority for stale
socket reclamation.
"""
import contextlib
import json
import os
import select
import socket
import struct
import threading
import time
from pathlib import Path

from . import config
from .warm_stdio_daemon import _warm_owner_marker_env


_PROTOCOL_VERSION = 1
_FRAME_HEADER = struct.Struct("!I")
_MAX_FRAME_BYTES = int(config._cfg_get("advisor", "codex_daemon_broker_max_frame_bytes", 16 * 1024 * 1024))
_CONNECT_TIMEOUT_S = float(config._cfg_get("advisor", "codex_daemon_broker_connect_timeout_s", 2.0))
_RESPONSE_GRACE_S = float(config._cfg_get("advisor", "codex_daemon_broker_response_grace_s", 5.0))
_LOCK_NAME = "codex-daemon"
_BROKER_PEER_PEEK_BYTES = 1
CODEX_BROKER_OWNER_LANE = "codex"


class _BrokerAttachImportFailure:
    def __init__(self, error):
        self.error = error


def _stale_consumer_attach_error(error):
    from . import __version__
    install_root = Path(__file__).resolve().parents[2]
    return {"error": (f"spendguard install in this venv is stale/broken (running {__version__}; "
                      f"attach-client import failed: {error}) — run `pip install -e {install_root}`; "
                      "uncounted local fallback refused")}


@contextlib.contextmanager
def _broker_request_ledger_lifetime(connection):
    """Interrupt this request's ledger reads when its peer abandons the socket.

    Only the serving thread consumes the framed protocol. The observer peeks at
    EOF and never changes socket timeouts or worker recycling policy.
    """
    from .ledger_read import ledger_request_scope

    observer_stopped = threading.Event()
    with ledger_request_scope() as request_reads:
        def observe_broker_peer_abandonment():
            while not observer_stopped.is_set():
                try:
                    readable, _, _ = select.select(
                        [connection], [], [], config.LEDGER_REQUEST_DISCONNECT_POLL_S)
                    if readable:
                        if not connection.recv(_BROKER_PEER_PEEK_BYTES, socket.MSG_PEEK):
                            request_reads.cancel_ledger_request()
                            return
                        # No second frame is expected. Avoid spinning if a peer
                        # pipelined data; the ledger deadline remains in force.
                        observer_stopped.wait(config.LEDGER_REQUEST_DISCONNECT_POLL_S)
                except (OSError, ValueError, TypeError):
                    request_reads.cancel_ledger_request()
                    return

        observer = threading.Thread(target=observe_broker_peer_abandonment,
                                    name="codex-broker-ledger-abandonment", daemon=True)
        observer.start()
        try:
            yield
        finally:
            observer_stopped.set()
            observer.join()


def _socket_path():
    configured = config._cfg_get("advisor", "codex_daemon_socket_path", None)
    path = Path(os.path.expanduser(os.fspath(configured))) if configured else config.HOME / "codex-daemon.sock"
    return path if path.is_absolute() else config.HOME / path


def _receive_exact(connection, size, deadline=None):
    chunks = []
    remaining = size
    while remaining:
        if deadline is not None:
            left = deadline - time.monotonic()
            if left <= 0:
                raise TimeoutError("codex broker request-read deadline expired")
            connection.settimeout(left)
        chunk = connection.recv(remaining)
        if not chunk:
            raise ConnectionError("broker connection closed mid-frame")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _receive_frame(connection, deadline=None):
    size = _FRAME_HEADER.unpack(_receive_exact(connection, _FRAME_HEADER.size, deadline))[0]
    if size > _MAX_FRAME_BYTES:
        raise ValueError("broker frame exceeds configured limit")
    value = json.loads(_receive_exact(connection, size, deadline).decode("utf-8"))
    if not isinstance(value, dict):
        raise ValueError("broker frame is not an object")
    return value


def _send_frame(connection, value):
    # Carry dispatch identity on the wire, rather than relying on thread-local
    # context surviving a remote broker or its reader threads.
    if value.get("method") == "run_warm":
        from . import execution_journal
        execution_id = execution_journal.current_durable_execution_id()
        if execution_id is not None:
            value = {**value, "execution": {
                "id": execution_id,
                "root": os.fspath(execution_journal.current_durable_execution_root()
                                  or execution_journal.durable_execution_store_root())}}
    payload = json.dumps(value, separators=(",", ":")).encode("utf-8")
    if len(payload) > _MAX_FRAME_BYTES:
        raise ValueError("broker frame exceeds configured limit")
    connection.sendall(_FRAME_HEADER.pack(len(payload)) + payload)


class _CodexDaemonBroker:
    """Bound-method facade preserving the codex_daemon module API while sharing one host owner."""

    def __init__(self, local_daemon, owner_marker=None):
        self._local = local_daemon
        self._owner_marker = dict(owner_marker or {})
        self._role_lock = threading.Lock()
        self._owner_pid = None
        self._lock_context = None
        self._listener = None
        self._stopping = threading.Event()
        self._handler_slots = threading.BoundedSemaphore(
            config.codex_daemon_pool_settings()["size"] + config.codex_daemon_admission_settings()["max_waiters"])
        if hasattr(os, "register_at_fork"):
            os.register_at_fork(after_in_child=self._reset_in_forked_child)

    def _reset_in_forked_child(self):
        """Discard inherited owner descriptors; only the process that acquired the lock may act as owner."""
        if self._listener is not None:
            self._listener.close()
        if self._lock_context is not None:
            self._lock_context.__exit__(None, None, None)
        self._role_lock = threading.Lock()
        self._owner_pid = None
        self._lock_context = None
        self._listener = None
        self._stopping = threading.Event()
        self._handler_slots = threading.BoundedSemaphore(
            config.codex_daemon_pool_settings()["size"] + config.codex_daemon_admission_settings()["max_waiters"])

    def _become_owner_if_available(self):
        current_marker = _warm_owner_marker_env(CODEX_BROKER_OWNER_LANE)
        from .codex_broker_owner import launchd_codex_broker_owner_supported
        from .warm_stdio_daemon import WARM_OWNER_BOOT_ENV, WARM_OWNER_PID_ENV
        dedicated_owner_required = launchd_codex_broker_owner_supported()
        marker_matches_process = (
            WARM_OWNER_BOOT_ENV in self._owner_marker
            and WARM_OWNER_PID_ENV in self._owner_marker
            and self._owner_marker == current_marker
        )
        if dedicated_owner_required and not marker_matches_process:
            return False
        pid = os.getpid()
        with self._role_lock:
            if self._owner_pid == pid and self._listener is not None:
                return True
            lock_context = config.single_instance_lock(_LOCK_NAME)
            try:
                acquired = lock_context.__enter__()
            except Exception:
                return False
            if not acquired:
                lock_context.__exit__(None, None, None)
                return False
            listener = None
            path = _socket_path()
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                try:
                    path.unlink()
                except FileNotFoundError:
                    pass
                listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                listener.bind(str(path))
                listener.listen()
                listener.settimeout(0.5)
            except Exception as exc:
                if listener is not None:
                    listener.close()
                lock_context.__exit__(None, None, None)
                config.warn_once("[spendguard] codex daemon broker: owner socket unavailable (%s); refusing local fallback"
                                 % str(exc)[:160])
                return False
            self._owner_pid = pid
            self._lock_context = lock_context
            self._listener = listener
            self._stopping.clear()
            self._local.ensure_running()
            threading.Thread(target=self._serve_connections, name="codex-daemon-broker", daemon=True).start()
            return True

    def _serve_connections(self):
        while not self._stopping.is_set():
            try:
                connection, _ = self._listener.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            if not self._handler_slots.acquire(blocking=False):
                with contextlib.closing(connection):
                    try:
                        connection.settimeout(config.codex_daemon_admission_settings()["request_read_timeout_s"])
                        _send_frame(connection, {"version": _PROTOCOL_VERSION,
                                                "result": {"error": "codex broker waiter limit reached"}})
                    except OSError:
                        pass
                continue
            try:
                threading.Thread(target=self._serve_one, args=(connection,), daemon=True).start()
            except BaseException:
                connection.close()
                self._handler_slots.release()
                raise

    def _serve_one(self, connection):
        from . import execution_journal
        with contextlib.closing(connection):
            try:
                read_timeout = config.codex_daemon_admission_settings()["request_read_timeout_s"]
                request = _receive_frame(connection, time.monotonic() + read_timeout)
                connection.settimeout(read_timeout)
                method = request.get("method")
                if request.get("version") != _PROTOCOL_VERSION or method not in ("run_warm", "read_rate_limits"):
                    _send_frame(connection, {"version": _PROTOCOL_VERSION, "protocol_error": True})
                    return
                kwargs = request.get("kwargs")
                if not isinstance(kwargs, dict):
                    raise ValueError("broker kwargs are not an object")
                connection.settimeout(None)
                execution = request.get("execution") or {}
                with _broker_request_ledger_lifetime(connection), execution_journal.durable_execution_scope(
                        execution.get("id"), root=execution.get("root")):
                    result = (self._local.run_warm(**kwargs) if method == "run_warm"
                              else self._local.read_rate_limits(**kwargs))
                connection.settimeout(read_timeout)
                _send_frame(connection, {"version": _PROTOCOL_VERSION, "result": result})
            except execution_journal.DurableExecutionPersistenceError as exc:
                connection.settimeout(config.codex_daemon_admission_settings()["request_read_timeout_s"])
                _send_frame(connection, {"version": _PROTOCOL_VERSION, "persistence_error": str(exc)})
            except ValueError as exc:
                connection.settimeout(config.codex_daemon_admission_settings()["request_read_timeout_s"])
                _send_frame(connection, {"version": _PROTOCOL_VERSION,
                                         "result": {"error": f"codex broker invalid request refused: {exc}"}})
            except Exception as exc:
                config.warn_once("[spendguard] codex daemon broker: request failed (%s); refusing request"
                                 % str(exc)[:160])
                try:
                    connection.settimeout(config.codex_daemon_admission_settings()["request_read_timeout_s"])
                    _send_frame(connection, {"version": _PROTOCOL_VERSION,
                                            "result": {"error": f"codex broker request refused: {exc}"}})
                except Exception:
                    pass
            finally:
                self._handler_slots.release()

    def _call_owner(self, kwargs, method="run_warm"):
        deadline = time.monotonic() + _CONNECT_TIMEOUT_S
        while True:
            try:
                connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                connection.settimeout(max(0.1, deadline - time.monotonic()))
                connection.connect(str(_socket_path()))
                call_timeout = kwargs.get("timeout")
                connection.settimeout((float(call_timeout) if call_timeout and call_timeout > 0
                                       else float(self._local.CALL_TIMEOUT_S)) + _RESPONSE_GRACE_S
                                      + getattr(self._local, "STARTUP_TIMEOUT_S", 0))
                with contextlib.closing(connection):
                    _send_frame(connection, {"version": _PROTOCOL_VERSION, "method": method, "kwargs": kwargs})
                    response = _receive_frame(connection)
                if "persistence_error" in response:
                    from .execution_journal import DurableExecutionPersistenceError
                    raise DurableExecutionPersistenceError(response["persistence_error"])
                if response.get("version") != _PROTOCOL_VERSION:
                    return _BrokerAttachImportFailure(
                        ImportError(f"broker protocol {response.get('version')} is incompatible with client "
                                    f"protocol {_PROTOCOL_VERSION}"))
                if "result" not in response:
                    return None
                result = response["result"]
                return result if isinstance(result, dict) else None
            except ValueError as exc:
                connection.close()
                return {"error": f"codex broker invalid response refused: {exc}"}
            except ImportError as exc:
                try:
                    connection.close()
                except Exception:
                    pass
                return _BrokerAttachImportFailure(exc)
            except (OSError, TimeoutError):
                try:
                    connection.close()
                except Exception:
                    pass
                if time.monotonic() >= deadline:
                    return None
                time.sleep(min(0.02, max(0.0, deadline - time.monotonic())))
            except Exception as exc:
                try:
                    connection.close()
                except Exception:
                    pass
                from .execution_journal import DurableExecutionPersistenceError
                if isinstance(exc, DurableExecutionPersistenceError):
                    raise
                return None

    def broker_run_warm(self, prompt, model=None, thread=None, reasoning=None, sandbox="read-only", cwd=None,
                        timeout=None, recycle_on_timeout=None):
        if os.environ.get(config.CODEX_LEASE_ACTIVE_ENV):
            config.warn_once("[spendguard] nested-codex-lease -> cold-exec divert")
            from . import codex_exec
            return codex_exec.run_cold_prompt(
                prompt, model=model, thread=thread, reasoning=reasoning, sandbox=sandbox, cwd=cwd,
                timeout=timeout, recycle_on_timeout=recycle_on_timeout)
        kwargs = {"prompt": prompt, "model": model, "thread": thread, "reasoning": reasoning,
                  "sandbox": sandbox, "cwd": os.fspath(cwd) if cwd is not None else None, "timeout": timeout,
                  "recycle_on_timeout": recycle_on_timeout}
        if self._become_owner_if_available():
            return self._local.run_warm(**kwargs)
        result = self._call_owner(kwargs)
        if isinstance(result, _BrokerAttachImportFailure):
            return _stale_consumer_attach_error(result.error)
        if result is None:
            from .codex_broker_owner import ensure_codex_broker_owner_running
            ensure_codex_broker_owner_running()
            result = self._call_owner(kwargs)
        if isinstance(result, _BrokerAttachImportFailure):
            return _stale_consumer_attach_error(result.error)
        return result if result is not None else {"error": "codex broker unreachable; uncounted local fallback refused"}

    def broker_read_rate_limits(self, timeout=None):
        kwargs = {"timeout": timeout}
        if self._become_owner_if_available():
            return self._local.read_rate_limits(**kwargs)
        result = self._call_owner(kwargs, method="read_rate_limits")
        if isinstance(result, _BrokerAttachImportFailure):
            return {"rate_limits": None, "error": str(result.error)}
        if result is None:
            from .codex_broker_owner import ensure_codex_broker_owner_running
            ensure_codex_broker_owner_running()
            result = self._call_owner(kwargs, method="read_rate_limits")
        if isinstance(result, _BrokerAttachImportFailure):
            return {"rate_limits": None, "error": str(result.error)}
        return result if result is not None else {
            "rate_limits": None, "error": "codex warm-pool broker unavailable"}

    def broker_ensure_running(self):
        if self._become_owner_if_available():
            return self._local.ensure_running()
        if self.broker_running():
            return True
        from .codex_broker_owner import ensure_codex_broker_owner_running
        ensure_codex_broker_owner_running()
        if self.broker_running():
            return True
        config.warn_once("[spendguard] codex broker unavailable; uncounted local startup refused")
        return False

    def broker_owner_serve_forever(self):
        """Build and serve only when this facade carries this process's exact owner identity."""
        if not self._become_owner_if_available():
            return False
        from . import release
        while not self._stopping.wait(config.codex_daemon_pool_settings()["health_interval_s"]):
            release.exit_if_stale(config.warn_once)
        return True

    def broker_running(self):
        if self._owner_pid == os.getpid():
            return self._local.running()
        connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            connection.settimeout(_CONNECT_TIMEOUT_S)
            connection.connect(str(_socket_path()))
            return True
        except Exception:
            return False
        finally:
            connection.close()

    def broker_shutdown(self):
        with self._role_lock:
            if self._owner_pid != os.getpid():
                self._local.shutdown()
                return
            self._stopping.set()
            listener, self._listener = self._listener, None
            lock_context, self._lock_context = self._lock_context, None
            self._owner_pid = None
            if listener is not None:
                listener.close()
            try:
                _socket_path().unlink()
            except FileNotFoundError:
                pass
            self._local.shutdown()
            if lock_context is not None:
                lock_context.__exit__(None, None, None)
