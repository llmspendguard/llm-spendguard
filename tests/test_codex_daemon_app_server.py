#!/usr/bin/env python3
import os
import pathlib
from concurrent.futures import ThreadPoolExecutor

from spendguard import codex_daemon


FAKE = pathlib.Path(__file__).with_name("fake_codex_app_server.py").resolve()


def new_daemon():
    os.environ["SPENDGUARD_CODEX_BIN"] = str(FAKE)
    return codex_daemon._CodexDaemon()


def test_agent_message():
    daemon = new_daemon()
    try:
        result = daemon.run_warm("hello", model="openai:test", reasoning="minimal")
        assert result == {"text": "reply:hello", "thread": "thread-1", "error": None}, result
    finally:
        daemon.shutdown()


def test_concurrent_turns_are_demultiplexed():
    daemon = new_daemon()
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(daemon.run_warm, "first")
            second = pool.submit(daemon.run_warm, "second")
            results = [first.result(timeout=5), second.result(timeout=5)]
        assert [result["text"] for result in results] == ["reply:first", "reply:second"], results
        assert results[0]["thread"] != results[1]["thread"], results
    finally:
        daemon.shutdown()


def test_failed_turn_is_a_hard_error():
    daemon = new_daemon()
    try:
        result = daemon.run_warm("fail")
        assert result["text"] is None, result
        assert result["error"], result
        assert result["tool_error"] is True, result
    finally:
        daemon.shutdown()


def test_dead_pipe_wakes_every_waiter():
    daemon = new_daemon()
    try:
        process = daemon.ensure_running()
        waiters = [daemon._register_waiter(10_000 + index) for index in range(2)]
        process.terminate()
        process.wait(timeout=5)
        assert all(waiter["event"].wait(2) for waiter in waiters)
        assert all(waiter["dead"] for waiter in waiters)
    finally:
        daemon.shutdown()


if __name__ == "__main__":
    tests = [test_agent_message, test_concurrent_turns_are_demultiplexed,
             test_failed_turn_is_a_hard_error, test_dead_pipe_wakes_every_waiter]
    for test in tests:
        test()
        print(f"PASS {test.__name__}")
