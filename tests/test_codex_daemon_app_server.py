#!/usr/bin/env python3
import os
from unittest.mock import patch
import pathlib
from _script_checks import collect_daemon_concurrent_turns, collect_daemon_dead_pipe_waiters

from spendguard import config  # noqa: E402
from spendguard import codex_daemon


FAKE = pathlib.Path(__file__).with_name("fake_codex_app_server.py").resolve()


def new_daemon():
    os.environ["SPENDGUARD_CODEX_BIN"] = str(FAKE)
    return codex_daemon._CodexDaemon()


@patch.dict(os.environ)
def test_agent_message():
    os.environ.pop(config.CLI_TEST_OFFLINE_ENVIRONMENT_KEY, None)
    daemon = new_daemon()
    try:
        result = daemon.run_warm("hello", model="openai:test", reasoning="minimal")
        assert result == {"text": "reply:hello", "thread": "thread-1", "turn": "turn-1",
                          "process_pid": daemon._proc.pid, "error": None}, result
    finally:
        daemon.shutdown()


@patch.dict(os.environ)
def test_concurrent_turns_are_demultiplexed():
    os.environ.pop(config.CLI_TEST_OFFLINE_ENVIRONMENT_KEY, None)
    results = collect_daemon_concurrent_turns(new_daemon, ("first", "second"), timeout=5)
    assert [result["text"] for result in results] == ["reply:first", "reply:second"], results
    assert results[0]["thread"] != results[1]["thread"], results


@patch.dict(os.environ)
def test_codex_failed_turn_is_error():
    os.environ.pop(config.CLI_TEST_OFFLINE_ENVIRONMENT_KEY, None)
    daemon = new_daemon()
    try:
        result = daemon.run_warm("fail")
        assert result["text"] is None, result
        assert result["error"], result
        assert result["tool_error"] is True, result
    finally:
        daemon.shutdown()


@patch.dict(os.environ)
def test_real_notifications_racing_before_turn_response_are_replayed():
    os.environ.pop(config.CLI_TEST_OFFLINE_ENVIRONMENT_KEY, None)
    daemon = new_daemon()
    try:
        result = daemon.run_warm("race")
        assert result == {"text": "reply:race", "thread": "thread-1", "turn": "turn-1",
                          "process_pid": daemon._proc.pid, "error": None}, result
    finally:
        daemon.shutdown()


@patch.dict(os.environ)
def test_dead_pipe_wakes_every_waiter():
    os.environ.pop(config.CLI_TEST_OFFLINE_ENVIRONMENT_KEY, None)
    awakened, dead = collect_daemon_dead_pipe_waiters(
        new_daemon, range(10_000, 10_002), process_timeout=5, event_timeout=2)
    assert awakened
    assert dead


if __name__ == "__main__":
    tests = [test_agent_message, test_concurrent_turns_are_demultiplexed,
             test_real_notifications_racing_before_turn_response_are_replayed,
             test_codex_failed_turn_is_error, test_dead_pipe_wakes_every_waiter]
    for test in tests:
        test()
        print(f"PASS {test.__name__}")
