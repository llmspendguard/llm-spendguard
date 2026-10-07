"""Warm Codex lane (codex_daemon) — LIFECYCLE + codex_exec fallback. Offline, no real codex.

The JSON-RPC PROTOCOL flow (initialize → thread/start → turn/start, notification demux by (threadId, turnId),
concurrency, hard-error, dead-pipe wakeups) is covered by test_codex_daemon_app_server.py against a fake
`codex app-server`. THIS file guards the protocol-AGNOSTIC behaviour that survived the mcp-server → app-server
rewire (codex 0.160.1 removed `codex mcp-server`): _spawn degrades (never raises) on an unstartable binary; the warm
daemon is default-on with an env opt-out; and codex_exec.run_prompt falls back to a cold `codex exec` when the daemon
path fails — so enabling the daemon changes LATENCY, never AVAILABILITY.
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

print("\n-- daemon is default-on, opt-out works, and a workspace-write daemon failure falls back to cold exec --")
_o_daemon, _o_runwarm, _o_bin2, _o_run = _cx._daemon_enabled, cd.run_warm, _cx._bin, _cx.subprocess.run
_old_env = os.environ.pop("SPENDGUARD_CODEX_DAEMON", None)
try:
    fails += ck("daemon is enabled by default", _cx._daemon_enabled())
    os.environ["SPENDGUARD_CODEX_DAEMON"] = "0"
    fails += ck("SPENDGUARD_CODEX_DAEMON=0 opts out", not _cx._daemon_enabled())
    os.environ.pop("SPENDGUARD_CODEX_DAEMON")

    def _boom(*a, **k):
        raise RuntimeError("boom")
    cd.run_warm = _boom                       # the daemon path raises → run_prompt must catch + fall to cold exec
    _cx._bin = lambda: "/fake/codex"

    def _cold_ok(cmd, **kwargs):
        out_file = cmd[cmd.index("--output-last-message") + 1]
        with open(out_file, "w") as destination:
            destination.write("cold answer")
        return type("ColdResult", (), {"returncode": 0, "stdout": "", "stderr": ""})()
    _cx.subprocess.run = _cold_ok
    _out = _cx.run_prompt("hi", model="openai:gpt-5.5", sandbox="workspace-write")
    fails += ck("workspace-write daemon exception degrades cleanly to cold exec",
                _out.get("text") == "cold answer" and not _out.get("error"))
finally:
    _cx._daemon_enabled, cd.run_warm, _cx._bin, _cx.subprocess.run = _o_daemon, _o_runwarm, _o_bin2, _o_run
    if _old_env is not None:
        os.environ["SPENDGUARD_CODEX_DAEMON"] = _old_env
    else:
        os.environ.pop("SPENDGUARD_CODEX_DAEMON", None)

print(f"\n{'[FAIL]' if fails else 'OK'} test_codex_daemon: {len(fails)} failure(s)")
sys.exit(1 if fails else 0)
