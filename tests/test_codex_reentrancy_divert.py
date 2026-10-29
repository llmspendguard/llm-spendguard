import os
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from spendguard import codex_daemon, codex_exec, config
from spendguard.codex_daemon_broker import _CodexDaemonBroker


REENTRANCY_TEST_PROMPT = "offline reentrancy fixture"
REENTRANCY_TEST_MARKER_VALUE = "1"
REENTRANCY_TEST_COLD_RESULT = {"text": "cold sentinel", "thread": "cold-thread", "error": None}
REENTRANCY_TEST_WARM_RESULT = {"text": "warm sentinel", "thread": "warm-thread", "error": None}
REENTRANCY_TEST_TIMEOUT_S = 1
REENTRANCY_TEST_PID = 701


def reentrancy_fixture_broker(local_daemon):
    broker = object.__new__(_CodexDaemonBroker)
    broker._local = local_daemon
    return broker


def test_nested_codex_lease_diverts_to_tracked_cold_exec_without_pool_lease(monkeypatch):
    local_daemon = SimpleNamespace(run_warm=MagicMock(return_value=REENTRANCY_TEST_WARM_RESULT))
    broker = reentrancy_fixture_broker(local_daemon)
    monkeypatch.setattr(broker, "_become_owner_if_available", MagicMock(return_value=True))
    cold_run = MagicMock(return_value=REENTRANCY_TEST_COLD_RESULT)
    monkeypatch.setattr(codex_exec, "run_cold_prompt", cold_run)
    monkeypatch.setenv(config.CODEX_LEASE_ACTIVE_ENV, REENTRANCY_TEST_MARKER_VALUE)

    result = broker.broker_run_warm(REENTRANCY_TEST_PROMPT)

    assert result is REENTRANCY_TEST_COLD_RESULT
    cold_run.assert_called_once()
    broker._become_owner_if_available.assert_not_called()
    local_daemon.run_warm.assert_not_called()


def test_unmarked_codex_request_keeps_normal_warm_pool_path(monkeypatch):
    local_daemon = SimpleNamespace(run_warm=MagicMock(return_value=REENTRANCY_TEST_WARM_RESULT))
    broker = reentrancy_fixture_broker(local_daemon)
    monkeypatch.setattr(broker, "_become_owner_if_available", MagicMock(return_value=True))
    cold_run = MagicMock(return_value=REENTRANCY_TEST_COLD_RESULT)
    monkeypatch.setattr(codex_exec, "run_cold_prompt", cold_run)
    monkeypatch.delenv(config.CODEX_LEASE_ACTIVE_ENV, raising=False)

    result = broker.broker_run_warm(REENTRANCY_TEST_PROMPT)

    assert result is REENTRANCY_TEST_WARM_RESULT
    broker._become_owner_if_available.assert_called_once_with()
    local_daemon.run_warm.assert_called_once()
    cold_run.assert_not_called()


def test_codex_warm_and_cold_spawns_inherit_lease_marker():
    assert codex_daemon._CodexDaemon()._lease_marker_env() == {
        config.CODEX_LEASE_ACTIVE_ENV: REENTRANCY_TEST_MARKER_VALUE
    }
    captured_environment = {}
    child = MagicMock()
    child.__enter__.return_value = child
    child.pid = REENTRANCY_TEST_PID
    child.returncode = 0
    child.communicate.return_value = ("", "")
    child.poll.return_value = 0

    def capture_spawn(command, **kwargs):
        captured_environment.update(kwargs["env"])
        return child

    with patch.object(codex_exec, "_codex_stdio_reap_policy", return_value=([], set())), \
            patch.object(codex_exec.subprocess, "Popen", side_effect=capture_spawn), \
            patch("psutil.Process", side_effect=RuntimeError("offline process fixture")):
        codex_exec._run_cold_codex_with_reaper(
            [os.devnull], REENTRANCY_TEST_TIMEOUT_S, {}, os.curdir)

    assert captured_environment[config.CODEX_LEASE_ACTIVE_ENV] == REENTRANCY_TEST_MARKER_VALUE
