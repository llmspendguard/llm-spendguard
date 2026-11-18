"""Offline proof that Grok admission and warm leader sockets share one bounded capacity."""
from concurrent.futures import ThreadPoolExecutor
import os
import pathlib
import subprocess
import threading
from unittest.mock import patch

from spendguard import config
from spendguard.generate.execution import grok_exec
from spendguard.manage.token_operations import dispatch

TEST_POOL_CAPACITY = 2
TEST_CALL_COUNT_PAST_CAPACITY = TEST_POOL_CAPACITY + 1
TEST_WAIT_SECONDS = 2
TEST_GROK_EXECUTABLE = "/mock/grok"
TEST_AGENTIC_DIRECTORY = "/mock/workspace"
failures = []


def check(label, condition):
    if not condition:
        failures.append(label)
    print(f"  [{'OK' if condition else 'FAIL'}] {label}")


def successful_grok_process():
    return subprocess.CompletedProcess([], 0, stdout="ok\n", stderr="")


print("-- bounded leader sockets are distinct, waiting, and warm-reused --")
entered_sockets = []
entered_lock = threading.Lock()
capacity_reached = threading.Event()
release_calls = threading.Event()


def blocking_process_runner(command, **_kwargs):
    socket_flag_index = command.index(grok_exec.GROK_LEADER_SOCKET_FLAG)
    with entered_lock:
        entered_sockets.append(command[socket_flag_index + 1])
        if len(entered_sockets) == TEST_POOL_CAPACITY:
            capacity_reached.set()
    if not release_calls.wait(TEST_WAIT_SECONDS):
        raise TimeoutError("test release signal did not arrive")
    return successful_grok_process()


leader_pool = grok_exec._GrokLeaderSocketPool(TEST_POOL_CAPACITY)
try:
    with patch.object(grok_exec, "_bin", return_value=TEST_GROK_EXECUTABLE), patch.object(
        grok_exec, "_warm_grok_leader_pool", return_value=leader_pool
    ), patch.object(grok_exec.subprocess, "run", side_effect=blocking_process_runner), patch.object(
        config, "lane_exec_env", return_value={}
    ):
        with ThreadPoolExecutor(max_workers=TEST_CALL_COUNT_PAST_CAPACITY) as executor:
            futures = [executor.submit(grok_exec.run_prompt, f"prompt-{index}")
                       for index in range(TEST_CALL_COUNT_PAST_CAPACITY)]
            reached = capacity_reached.wait(TEST_WAIT_SECONDS)
            with entered_lock:
                initial_count = len(entered_sockets)
                initial_distinct_count = len(set(entered_sockets))
            release_calls.set()
            results = [future.result() for future in futures]
    check("pool reaches its configured capacity", reached and initial_count == TEST_POOL_CAPACITY)
    check("concurrent calls receive distinct leader sockets", initial_distinct_count == TEST_POOL_CAPACITY)
    check("call past capacity waits until a socket is released", len(entered_sockets) == TEST_CALL_COUNT_PAST_CAPACITY)
    check("released socket identity is reused instead of replaced", len(set(entered_sockets)) == TEST_POOL_CAPACITY)
    check("all mocked calls complete successfully", all(result["error"] is None for result in results))
finally:
    leader_pool.reap()


print("-- lean flags affect only non-agentic completion calls --")
commands = []
leader_pool = grok_exec._GrokLeaderSocketPool(TEST_POOL_CAPACITY)


def recording_process_runner(command, **_kwargs):
    commands.append(command)
    return successful_grok_process()


try:
    with patch.object(grok_exec, "_bin", return_value=TEST_GROK_EXECUTABLE), patch.object(
        grok_exec, "_warm_grok_leader_pool", return_value=leader_pool
    ), patch.object(grok_exec.subprocess, "run", side_effect=recording_process_runner), patch.object(
        config, "lane_exec_env", return_value={}
    ):
        non_agentic_result = grok_exec.run_prompt("completion")
        agentic_result = grok_exec.run_prompt("agent", agentic=True, cwd=TEST_AGENTIC_DIRECTORY)
    non_agentic_command, agentic_command = commands
    check("non-agentic completion uses a leader socket", grok_exec.GROK_LEADER_SOCKET_FLAG in non_agentic_command)
    check("non-agentic completion disables expensive agent facilities",
          all(flag in non_agentic_command for flag in grok_exec.GROK_NON_AGENTIC_FLAGS))
    check("non-agentic completion denies all built-in tools",
          non_agentic_command[-2:] == [grok_exec.GROK_BUILTIN_TOOLS_FLAG,
                                       grok_exec.GROK_DISABLED_BUILTIN_TOOLS])
    check("agentic workspace command keeps its prior tool behavior",
          grok_exec.GROK_LEADER_SOCKET_FLAG not in agentic_command
          and all(flag not in agentic_command for flag in grok_exec.GROK_NON_AGENTIC_FLAGS)
          and grok_exec.GROK_BUILTIN_TOOLS_FLAG not in agentic_command)
    check("both mocked command paths succeed", non_agentic_result["error"] is None and agentic_result["error"] is None)
finally:
    leader_pool.reap()


print("-- governor and executor consume one floored config value --")
original_configured_capacity = os.environ.get(config.GROK_LANE_CONCURRENCY_ENV)
try:
    os.environ[config.GROK_LANE_CONCURRENCY_ENV] = str(config.GROK_LANE_CONCURRENCY_FLOOR - 1)
    check("configured capacity cannot undercut the measured floor",
          config.grok_lane_concurrency() == config.GROK_LANE_CONCURRENCY_FLOOR
          and dispatch.effective_grok_concurrency() == config.GROK_LANE_CONCURRENCY_FLOOR)
    configured_above_floor = config.GROK_LANE_CONCURRENCY_FLOOR + 1
    os.environ[config.GROK_LANE_CONCURRENCY_ENV] = str(configured_above_floor)
    leader_pool = grok_exec._GrokLeaderSocketPool(config.grok_lane_concurrency())
    try:
        check("pool capacity equals governor capacity from the same override",
              leader_pool.capacity == dispatch.effective_grok_concurrency() == configured_above_floor)
        check("socket paths are isolated beneath the pool's process-temp directory",
              all(pathlib.Path(socket_path).parent == leader_pool._directory for socket_path in leader_pool._socket_paths))
    finally:
        leader_pool.reap()
finally:
    if original_configured_capacity is None:
        os.environ.pop(config.GROK_LANE_CONCURRENCY_ENV, None)
    else:
        os.environ[config.GROK_LANE_CONCURRENCY_ENV] = original_configured_capacity

print(f"\n{'[FAIL]' if failures else 'OK'} test_grok_lane_concurrency: {len(failures)} failure(s)")
raise SystemExit(1 if failures else 0)
