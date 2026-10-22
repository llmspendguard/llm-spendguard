"""F3 guard: two processes share one warm app-server through the host broker, without signalling its live pid."""
import multiprocessing
import os
from unittest.mock import patch
from spendguard import config
import pathlib
import socket
import sys
import tempfile
import threading
import time

os.environ.setdefault("SPENDGUARD_HOME", tempfile.mkdtemp(prefix="sg-codex-host-singleton-", dir="/tmp"))
os.environ.setdefault("SPENDGUARD_TEST_ISOLATED", "1")
os.environ.setdefault("SPENDGUARD_NO_AUTOINSTALL", "1")
os.environ["SPENDGUARD_CODEX_DAEMON_POOL_SIZE"] = "1"
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

FAKE_CODEX = pathlib.Path(__file__).with_name("fake_codex_app_server.py").resolve()
os.environ["SPENDGUARD_CODEX_BIN"] = str(FAKE_CODEX)

_SOCKETPAIR_OWNER = None
_SOCKETPAIR_CLIENT = None


class _SocketPairConnection:
    def __init__(self, endpoint):
        self._endpoint = endpoint

    def settimeout(self, timeout):
        self._endpoint.settimeout(timeout)

    def connect(self, path):
        return None

    def recv(self, size):
        return self._endpoint.recv(size)

    def sendall(self, payload):
        self._endpoint.sendall(payload)

    def close(self):
        self._endpoint.close()


class _SocketPairListener:
    def __init__(self, endpoint):
        self._endpoint = endpoint
        self._accepted = False
        self._lock = threading.Lock()

    def bind(self, path):
        return None

    def listen(self):
        return None

    def settimeout(self, timeout):
        return None

    def accept(self):
        with self._lock:
            if not self._accepted:
                self._accepted = True
                return _SocketPairConnection(self._endpoint), None
        time.sleep(0.02)
        raise socket.timeout()

    def close(self):
        return None


def _install_socketpair_transport(owner):
    endpoint = _SOCKETPAIR_OWNER if owner else _SOCKETPAIR_CLIENT
    first = True

    def unix_socket(family, kind):
        nonlocal first
        assert family == socket.AF_UNIX, family
        assert kind == socket.SOCK_STREAM, kind
        if owner and first:
            first = False
            return _SocketPairListener(endpoint)
        return _SocketPairConnection(endpoint)

    socket.socket = unix_socket


def _owner(ready, stop, reports, use_socketpair):
    from spendguard import codex_daemon
    from spendguard import warm_stdio_daemon
    if use_socketpair:
        _install_socketpair_transport(owner=True)

    real_popen = warm_stdio_daemon.subprocess.Popen

    def counted_popen(*args, **kwargs):
        reports.put(("spawn", os.getpid()))
        return real_popen(*args, **kwargs)

    warm_stdio_daemon.subprocess.Popen = counted_popen
    result = codex_daemon.run_warm("owner")
    reports.put(("owner_result", result))
    ready.set()
    stop.wait(10)
    codex_daemon.shutdown()


def _client(ready, reports, use_socketpair):
    ready.wait(10)
    from spendguard import codex_daemon
    from spendguard import warm_stdio_daemon
    if use_socketpair:
        _install_socketpair_transport(owner=False)

    attempts = {"popen": 0, "os_kill": 0, "terminate": 0, "kill": 0}

    def forbidden_popen(*args, **kwargs):
        attempts["popen"] += 1
        raise AssertionError("attaching client attempted to spawn an app-server")

    def forbidden_os_kill(*args, **kwargs):
        attempts["os_kill"] += 1
        raise AssertionError("attaching client attempted to signal a live pid")

    def forbidden_terminate(*args, **kwargs):
        attempts["terminate"] += 1
        raise AssertionError("attaching client attempted to terminate a live app-server")

    def forbidden_kill(*args, **kwargs):
        attempts["kill"] += 1
        raise AssertionError("attaching client attempted to kill a live app-server")

    popen_class = warm_stdio_daemon.subprocess.Popen
    popen_class.terminate = forbidden_terminate
    popen_class.kill = forbidden_kill
    warm_stdio_daemon.subprocess.Popen = forbidden_popen
    os.kill = forbidden_os_kill
    result = codex_daemon.run_warm("client")
    reports.put(("client_result", result))
    reports.put(("attach_attempts", attempts))


@patch.dict(os.environ)
def test_two_host_attempts_share_one_warm_daemon_without_signals():
    os.environ.pop(config.CLI_TEST_OFFLINE_ENVIRONMENT_KEY, None)
    global _SOCKETPAIR_OWNER, _SOCKETPAIR_CLIENT
    probe_path = os.path.join(os.environ["SPENDGUARD_HOME"], "codex-daemon-probe.sock")
    probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        probe.bind(probe_path)
        use_socketpair = False
    except PermissionError:
        use_socketpair = True
    finally:
        probe.close()
        try:
            os.unlink(probe_path)
        except FileNotFoundError:
            pass
    context = multiprocessing.get_context("fork" if use_socketpair else "spawn")
    if use_socketpair:
        _SOCKETPAIR_OWNER, _SOCKETPAIR_CLIENT = socket.socketpair()
    ready = context.Event()
    stop = context.Event()
    reports = context.Queue()
    owner = context.Process(target=_owner, args=(ready, stop, reports, use_socketpair))
    client = context.Process(target=_client, args=(ready, reports, use_socketpair))
    owner.start()
    client.start()
    client.join(15)
    stop.set()
    owner.join(15)
    if use_socketpair:
        _SOCKETPAIR_OWNER.close()
        _SOCKETPAIR_CLIENT.close()
    assert owner.exitcode == 0, owner.exitcode
    assert client.exitcode == 0, client.exitcode
    messages = [reports.get(timeout=2) for _ in range(4)]
    spawns = [message for message in messages if message[0] == "spawn"]
    owner_result = next(message[1] for message in messages if message[0] == "owner_result")
    client_result = next(message[1] for message in messages if message[0] == "client_result")
    attempts = next(message[1] for message in messages if message[0] == "attach_attempts")
    assert len(spawns) == 1, messages
    assert owner_result["text"] == "reply:owner", owner_result
    assert client_result["text"] == "reply:client", client_result
    assert attempts == {"popen": 0, "os_kill": 0, "terminate": 0, "kill": 0}, attempts


if __name__ == "__main__":
    test_two_host_attempts_share_one_warm_daemon_without_signals()
    print("PASS test_two_host_attempts_share_one_warm_daemon_without_signals")
