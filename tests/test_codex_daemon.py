"""Warm Codex lane (codex_daemon) — lifecycle and admission refusal. Offline, no real codex.

The JSON-RPC PROTOCOL flow (initialize → thread/start → turn/start, notification demux by (threadId, turnId),
concurrency, hard-error, dead-pipe wakeups) is covered by test_codex_daemon_app_server.py against a fake
`codex app-server`. THIS file guards the protocol-AGNOSTIC behaviour that survived the mcp-server → app-server
rewire (codex 0.160.1 removed `codex mcp-server`): _spawn degrades (never raises) on an unstartable binary; the warm
daemon is default-on with an env opt-out; and codex_exec.run_prompt refuses uncounted cold execution when
warm admission fails.
"""
import os
import sys
import tempfile

os.environ.setdefault("SPENDGUARD_HOME", tempfile.mkdtemp(prefix="sg-cdxd-"))
os.environ.setdefault("SPENDGUARD_TEST_ISOLATED", "1")
os.environ.setdefault("SPENDGUARD_NO_AUTOINSTALL", "1")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from spendguard import codex_daemon as cd        # noqa: E402
from spendguard import codex_exec as _cx          # noqa: E402


def ck(name, cond):
    ok = bool(cond)
    print(("  [OK] " if ok else "  [FAIL] ") + name)
    return [] if ok else [name]


fails = []

print("-- _spawn returns None (never raises) when the binary can't start --")
_o_bin, _o_flags = _cx._bin, _cx._plugin_disable_flags
try:
    _cx._bin = lambda: "/nonexistent/codex-does-not-exist"
    _cx._plugin_disable_flags = lambda: []
    fails += ck("_spawn → None on an unstartable binary", cd._CodexDaemon()._spawn() is None)
finally:
    _cx._bin, _cx._plugin_disable_flags = _o_bin, _o_flags

print("\n-- daemon is default-on, opt-out works, and warm failure refuses uncounted cold exec --")
_o_daemon, _o_runwarm, _o_bin2, _o_cold = (
    _cx._daemon_enabled, cd.run_warm, _cx._bin, _cx._run_cold_codex_with_reaper)
_old_env = os.environ.pop("SPENDGUARD_CODEX_DAEMON", None)
try:
    fails += ck("daemon is enabled by default", _cx._daemon_enabled())
    os.environ["SPENDGUARD_CODEX_DAEMON"] = "0"
    fails += ck("SPENDGUARD_CODEX_DAEMON=0 opts out", not _cx._daemon_enabled())
    os.environ.pop("SPENDGUARD_CODEX_DAEMON")

    def _boom(*a, **k):
        raise RuntimeError("boom")
    cd.run_warm = _boom                       # daemon exceptions must surface without starting a cold process
    _cx._bin = lambda: "/fake/codex"

    def _cold_ok(cmd, **kwargs):
        out_file = cmd[cmd.index("--output-last-message") + 1]
        with open(out_file, "w") as destination:
            destination.write("cold answer")
        return type("ColdResult", (), {"returncode": 0, "stdout": "", "stderr": ""})()
    # Workspace-write cold execution now owns descendant cleanup through this boundary.
    _cx._run_cold_codex_with_reaper = lambda cmd, **kwargs: _cold_ok(cmd, **kwargs)
    _out = _cx.run_prompt("hi", model="openai:gpt-5.5", sandbox="workspace-write", cwd=os.path.dirname(__file__))
    fails += ck("workspace-write daemon exception refuses uncounted cold exec",
                "boom" in _out.get("error", "") and not _out.get("text"))
finally:
    _cx._daemon_enabled, cd.run_warm, _cx._bin, _cx._run_cold_codex_with_reaper = (
        _o_daemon, _o_runwarm, _o_bin2, _o_cold)
    if _old_env is not None:
        os.environ["SPENDGUARD_CODEX_DAEMON"] = _old_env
    else:
        os.environ.pop("SPENDGUARD_CODEX_DAEMON", None)

print(f"\n{'[FAIL]' if fails else 'OK'} test_codex_daemon: {len(fails)} failure(s)")
sys.exit(1 if fails else 0)
