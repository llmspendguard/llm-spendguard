"""Guard: the HTTP MCP server pre-warms the codex app-server daemon without blocking startup.

Offline: daemon enablement and startup are monkeypatched, so no HTTP server, network, LLM, or real daemon starts.
"""
import os
import sys
import tempfile
import time

os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="sg-mcp-prewarm-")
os.environ["SPENDGUARD_NO_AUTOINSTALL"] = "1"
os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

import spendguard  # noqa: E402, F401
from spendguard import codex_daemon, codex_exec, mcp_server  # noqa: E402

fails = []


def ck(label, cond, detail=""):
    print(f"  [{'OK' if cond else 'FAIL'}] {label}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        fails.append(label)


called = []


def fake_ensure_running():
    called.append(True)


codex_daemon.ensure_running = fake_ensure_running

# ── enabled → start the daemon in the background ──
codex_exec._daemon_enabled = lambda: True
mcp_server._prewarm_codex_daemon()
deadline = time.time() + 2.0
while not called and time.time() < deadline:
    time.sleep(0.01)
ck("enabled codex daemon was pre-warmed", bool(called), called)

# ── disabled → return without starting a thread or daemon ──
called.clear()
codex_exec._daemon_enabled = lambda: False
mcp_server._prewarm_codex_daemon()
time.sleep(0.05)
ck("disabled codex daemon was not pre-warmed", not called, called)

print(f"\n{'[FAIL]' if fails else 'OK'} test_mcp_prewarm_codex_daemon: {len(fails)} failure(s)")
sys.exit(1 if fails else 0)
