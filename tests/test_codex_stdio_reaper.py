"""Offline process-table safety and cold-run failure isolation; never signals real processes."""
import os
import sys
import tempfile
import threading
import json
from functools import partial
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import psutil
from spendguard import codex_exec as ce

REAPER_TEST_COMMAND = sys.executable
REAPER_TEST_ARGS = ("-m", "fixture_stdio_server")
REAPER_TEST_PORT = 49123
REAPER_TEST_BIRTH = 42.0
REAPER_TEST_ROOT_PID = 100
REAPER_TEST_ORPHAN_PID = 101
REAPER_TEST_HTTP_PID = 102
REAPER_TEST_BOUND_PID = 103
REAPER_TEST_OWNED_PID = 104
REAPER_TEST_REUSED_PID = 105
REAPER_TEST_DENIED_PID = 106
REAPER_TEST_ANSWER = "offline completed"
REAPER_TEST_WAIT_S = 5


def reaper_assert_safety(label, condition):
    assert condition, label
    print("[OK] " + label)


def reaper_fixture_process(pid, argv=None, parent=ce.CODEX_ORPHAN_PARENT_PID, sockets=(), birth=REAPER_TEST_BIRTH):
    process = MagicMock()
    process.pid = pid
    process.cmdline.return_value = list(argv or (REAPER_TEST_COMMAND, *REAPER_TEST_ARGS))
    process.create_time.return_value = birth
    process.ppid.return_value = parent
    process.net_connections.return_value = sockets
    return process


def reaper_fixture_descendants(observed_event, descendant, recursive):
    assert recursive
    observed_event.set()
    return [descendant]


def reaper_fixture_spawn(child_process, answer, cmd, **kwargs):
    Path(cmd[cmd.index("--output-last-message") + 1]).write_text(answer)
    return child_process


def reaper_fixture_communicate(observed_event, timeout):
    assert observed_event.wait(REAPER_TEST_WAIT_S), "descendant capture did not run"
    return "", ""


with tempfile.TemporaryDirectory() as config_home:
    config_path = Path(config_home) / ce.CODEX_CONFIG_FILENAME
    # Config is fixture input, including the transport and port, not production signatures.
    config_path.write_text(
        '[mcp_servers.stdio]\ncommand = ' + json.dumps(REAPER_TEST_COMMAND) + '\nargs = '
        + json.dumps(REAPER_TEST_ARGS) + '\n[mcp_servers.http]\nurl = '
        + json.dumps(f"http://127.0.0.1:{REAPER_TEST_PORT}/mcp") + '\n')
    policy = ce._codex_stdio_reap_policy({"CODEX_HOME": config_home})
    reaper_assert_safety("runtime TOML derives STDIO signatures and URL ports",
                         policy == ([(os.path.realpath(REAPER_TEST_COMMAND), REAPER_TEST_ARGS)], {REAPER_TEST_PORT}))
    orphan = reaper_fixture_process(REAPER_TEST_ORPHAN_PID)
    http = reaper_fixture_process(REAPER_TEST_HTTP_PID, argv=(REAPER_TEST_COMMAND, "-m", "fixture_http_service"))
    bound = reaper_fixture_process(REAPER_TEST_BOUND_PID, sockets=[SimpleNamespace(
        status=psutil.CONN_ESTABLISHED, laddr=SimpleNamespace(port=REAPER_TEST_PORT))])
    owned = reaper_fixture_process(REAPER_TEST_OWNED_PID, parent=REAPER_TEST_ROOT_PID)
    reused = reaper_fixture_process(REAPER_TEST_REUSED_PID, birth=REAPER_TEST_BIRTH + 1)
    denied = reaper_fixture_process(REAPER_TEST_DENIED_PID)
    denied.net_connections.side_effect = psutil.AccessDenied(REAPER_TEST_DENIED_PID)
    processes = {proc.pid: proc for proc in (orphan, http, bound, owned, reused, denied)}
    captured = dict.fromkeys(processes, REAPER_TEST_BIRTH)
    with patch.object(psutil, "Process", side_effect=processes.__getitem__):
        reaper_assert_safety("only captured orphaned command server selected", ce._reap_captured_codex_stdio(captured, policy) == 1)
        reaper_assert_safety("HTTP URL service spared", not http.terminate.called)
        reaper_assert_safety("configured MCP port spared even without LISTEN status", not bound.terminate.called)
        reaper_assert_safety("live owner spared", not owned.terminate.called)
        reaper_assert_safety("PID reuse and inspection uncertainty spared", not reused.terminate.called and not denied.terminate.called)
        orphan.cmdline.side_effect = psutil.NoSuchProcess(orphan.pid)
        reaper_assert_safety("repeat cleanup idempotent", ce._reap_captured_codex_stdio(captured, policy) == 0)
        orphan.cmdline.side_effect = None
        orphan.wait.side_effect = [psutil.TimeoutExpired(ce.CODEX_MCP_EXIT_WAIT_S), 0]
        reaper_assert_safety("TERM-resistant orphan rechecked then killed", ce._reap_captured_codex_stdio(captured, policy) == 1 and orphan.kill.called)
        orphan.kill.reset_mock()
        orphan.wait.side_effect = psutil.TimeoutExpired(ce.CODEX_MCP_EXIT_WAIT_S)
        orphan.ppid.side_effect = [ce.CODEX_ORPHAN_PARENT_PID, REAPER_TEST_ROOT_PID]
        reaper_assert_safety("escalation spares a newly live owner", ce._reap_captured_codex_stdio(captured, policy) == 0 and not orphan.kill.called)

    child = MagicMock()
    child.__enter__.return_value = child
    child.pid = REAPER_TEST_ROOT_PID
    child.returncode = 0
    child.poll.return_value = 0
    root = MagicMock()
    observed = threading.Event()
    root.children.side_effect = partial(reaper_fixture_descendants, observed, owned)
    child.communicate.side_effect = partial(reaper_fixture_communicate, observed)
    with patch.dict(os.environ, {"CODEX_HOME": config_home}), \
            patch.object(ce, "_bin", return_value=REAPER_TEST_COMMAND), \
            patch.object(ce, "_plugin_disable_flags", return_value=[]), \
            patch.object(ce.subprocess, "Popen", side_effect=partial(reaper_fixture_spawn, child, REAPER_TEST_ANSWER)), \
            patch.object(psutil, "Process", return_value=root), \
            patch.object(ce, "_reap_captured_codex_stdio", side_effect=RuntimeError("offline cleanup fault")) as cleanup:
        with tempfile.TemporaryDirectory() as output_home:
            output_path = str(Path(output_home) / "answer")
            result = ce._run_cold_codex_with_reaper(
                [REAPER_TEST_COMMAND, "--output-last-message", output_path],
                timeout=REAPER_TEST_WAIT_S, env={"CODEX_HOME": config_home}, cwd=config_home)
        reaper_assert_safety("reaper exception preserves successful utility result", result.returncode == 0)
        reaper_assert_safety("run captures descendant identities before cleanup", cleanup.call_args.args[0] == {owned.pid: REAPER_TEST_BIRTH})

print("OK test_codex_stdio_reaper")
