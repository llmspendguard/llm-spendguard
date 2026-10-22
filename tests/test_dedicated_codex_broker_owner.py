"""Anti-amnesia guards for dedicated Codex broker ownership. Offline: no socket bind, launchd, or app-server."""
import os
import sys
import tempfile
from unittest.mock import patch


if os.environ.get("SPENDGUARD_TEST_ISOLATED") != "1":
    isolated_home = tempfile.mkdtemp(prefix="sg-codex-owner-")
    isolated_environment = dict(os.environ)
    isolated_environment["SPENDGUARD_TEST_ISOLATED"] = "1"
    isolated_environment["SPENDGUARD_HOME"] = isolated_home
    isolated_environment["SPENDGUARD_NO_AUTOINSTALL"] = "1"
    os.execve(sys.executable, [sys.executable, __file__], isolated_environment)

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from spendguard import codex_broker_owner, codex_daemon_broker, warm_stdio_daemon  # noqa: E402
from _script_checks import record_script_check  # noqa: E402


failures = []


class OfflinePool:
    CALL_TIMEOUT_S = 1
    STARTUP_TIMEOUT_S = 1

    def __init__(self):
        self.starts = 0

    def ensure_running(self):
        self.starts += 1
        return object()

    def running(self):
        return False

    def shutdown(self):
        return None


def _simulate_dedicated_owner_test_platform(launchd_owner_supported):
    codex_broker_owner.launchd_codex_broker_owner_supported = lambda: launchd_owner_supported


class OfflineDedicatedOwnerListener:
    def bind(self, _path):
        return None

    def listen(self):
        return None

    def settimeout(self, _timeout):
        return None

    def close(self):
        return None


class OfflineDedicatedOwnerServerThread:
    def __init__(self, **_kwargs):
        pass

    def start(self):
        return None


class OfflineDedicatedOwnerRoleLock:
    def __init__(self, ownership_state):
        self._ownership_state = ownership_state

    def __enter__(self):
        if self._ownership_state["acquired"]:
            return False
        self._ownership_state["acquired"] = True
        return True

    def __exit__(self, _exc_type, _exc_value, _traceback):
        return None


# A consumer with no owner must request launchd ownership and must never build its local pool.
_simulate_dedicated_owner_test_platform(launchd_owner_supported=True)
consumer_pool = OfflinePool()
consumer = codex_daemon_broker._CodexDaemonBroker(consumer_pool)
owner_requests = []
codex_broker_owner.ensure_codex_broker_owner_running = lambda: owner_requests.append(True) or True
consumer.broker_running = lambda: len(owner_requests) > 0
record_script_check(failures, "consumer requests owner and attaches", consumer.broker_ensure_running() is True)
record_script_check(failures, "consumer never builds local pool", consumer_pool.starts == 0)

# Owner authority is exact PID+create-time marker identity; a healthy second start reuses the existing role.
owner_pool = OfflinePool()
owner_marker = warm_stdio_daemon._warm_owner_marker_env(codex_daemon_broker.CODEX_BROKER_OWNER_LANE)
owner = codex_daemon_broker._CodexDaemonBroker(owner_pool, owner_marker=owner_marker)
owner._owner_pid = os.getpid()
owner._listener = object()
record_script_check(failures, "matching marker recognizes existing owner", owner._become_owner_if_available() is True)
record_script_check(failures, "second owner start is a no-op", owner_pool.starts == 0)
wrong_marker = dict(owner_marker)
wrong_marker[warm_stdio_daemon.WARM_OWNER_BOOT_ENV] = "0"
wrong_owner = codex_daemon_broker._CodexDaemonBroker(OfflinePool(), owner_marker=wrong_marker)
record_script_check(failures, "mismatched marker cannot own", wrong_owner._become_owner_if_available() is False)

# Without a dedicated launchd owner, one markerless facade may build and a second shares that owner.
_simulate_dedicated_owner_test_platform(launchd_owner_supported=False)
fallback_owner_pool = OfflinePool()
fallback_consumer_pool = OfflinePool()
fallback_owner = codex_daemon_broker._CodexDaemonBroker(fallback_owner_pool)
fallback_consumer = codex_daemon_broker._CodexDaemonBroker(fallback_consumer_pool)
fallback_ownership_state = {"acquired": False}


def offline_dedicated_owner_role_lock(_lock_name):
    return OfflineDedicatedOwnerRoleLock(fallback_ownership_state)


with (patch.object(codex_daemon_broker.config, "single_instance_lock", offline_dedicated_owner_role_lock),
      patch.object(codex_daemon_broker.socket, "socket", return_value=OfflineDedicatedOwnerListener()),
      patch.object(codex_daemon_broker.threading, "Thread", OfflineDedicatedOwnerServerThread)):
    fallback_owner_acquired = fallback_owner._become_owner_if_available()
    fallback_consumer.broker_running = lambda: fallback_owner_acquired
    fallback_consumer_attached = fallback_consumer.broker_ensure_running()

record_script_check(failures, "markerless non-launchd consumer can own", fallback_owner_acquired is True)
record_script_check(failures, "non-launchd consumers share one local pool",
                    fallback_consumer_attached is True
                    and fallback_owner_pool.starts == 1
                    and fallback_consumer_pool.starts == 0)

# Unknown owner identity is never proof of death; only a missing PID or recycled identity can be reaped.
original_process = warm_stdio_daemon.psutil.Process
class UnknownIdentityProcess:
    def __init__(self, _pid):
        pass

    def create_time(self):
        raise PermissionError("identity unavailable")


warm_stdio_daemon.psutil.Process = UnknownIdentityProcess
record_script_check(failures, "unknown owner is not dead",
                    warm_stdio_daemon._warm_owner_provably_dead(os.getpid(), owner_marker.get(
                        warm_stdio_daemon.WARM_OWNER_BOOT_ENV)) is False)
warm_stdio_daemon.psutil.Process = original_process

print("\n%s test_dedicated_codex_broker_owner: %d failure(s)" %
      ("[FAIL]" if failures else "OK", len(failures)))
raise SystemExit(1 if failures else 0)
