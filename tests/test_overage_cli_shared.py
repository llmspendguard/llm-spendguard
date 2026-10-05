"""CLI and MCP overage surfaces call one shared status implementation."""
import os
import sys
import tempfile

os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="sg-overage-cli-")
os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"
os.environ["SPENDGUARD_NO_AUTOINSTALL"] = "1"
os.environ["OPENAI_API_KEY"] = "sk-test-fake-not-real"
os.environ["ANTHROPIC_API_KEY"] = "sk-ant-test-fake-not-real"
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from spendguard import cli, mcp_server, overage  # noqa: E402

seen = []
original = overage.current_overage_status


def shared_status_probe():
    seen.append("called")
    return {"on_overage_now": False, "meaning": "shared", "observed_overage_windows": 0,
            "real_overage_by_month_usd": {}}


overage.current_overage_status = shared_status_probe
try:
    overage.overage_cli([])
    mcp_server._tool_overage_status({})
finally:
    overage.current_overage_status = original

registered = any(command == "overage" for _group, entries in cli._GROUPS for command, _help in entries)
failures = []
if len(seen) != 2:
    failures.append("CLI and MCP did not both call the shared function")
if not registered:
    failures.append("overage command is not registered in CLI help")
print(f"{'[FAIL]' if failures else 'OK'} test_overage_cli_shared: {len(failures)} failure(s)")
sys.exit(1 if failures else 0)
