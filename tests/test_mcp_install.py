"""Guard: `spendguard install-mcp` registers spendguard as a stdio MCP server in the Claude Code client config
(~/.claude.json) WITHOUT clobbering the servers already there, is idempotent, and is fully reversible. The
registration must be ADDITIVE and the write parse-safe — its failure mode (silently dropping a user's other MCP
servers, or replacing an unparseable config with an empty one) is exactly the class config.update_json exists to
prevent, so this pins that install() actually routes through it. Hermetic: Path.home is redirected to a tmpdir,
so nothing touches the real ~/.claude.json; no network, no ledger, no spend."""
import os
import sys
import json
import tempfile
import pathlib

if not os.environ.get("SPENDGUARD_TEST_ISOLATED"):
    os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"
    os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="spendguard-mcpinstall-")
    os.execv(sys.executable, [sys.executable] + sys.argv)

from spendguard import mcp_server

fails = []


def ck(name, cond):
    print(("  [OK] " if cond else "  [FAIL] ") + name)
    if not cond:
        fails.append(name)


home = tempfile.mkdtemp(prefix="spendguard-clienthome-")
_orig_home = pathlib.Path.home
pathlib.Path.home = staticmethod(lambda: pathlib.Path(home))          # redirect ~ → the tmp client home
cfgp = pathlib.Path(home) / ".claude.json"

try:
    # a realistic pre-existing client config: ANOTHER MCP server + unrelated top-level keys the user depends on
    cfgp.write_text(json.dumps({
        "mcpServers": {"symgrep": {"type": "stdio", "command": "/x/symgrep-mcp", "args": [], "env": {}}},
        "numStartups": 42, "theme": "dark"}))

    print("-- register --")
    mcp_server.install()
    cfg = json.loads(cfgp.read_text())
    ck("spendguard is registered", "spendguard" in cfg["mcpServers"])
    ck("the entry mirrors the sibling schema (type=stdio, args=['mcp'], resolved command)",
       cfg["mcpServers"]["spendguard"]["type"] == "stdio"
       and cfg["mcpServers"]["spendguard"]["args"] == ["mcp"]
       and cfg["mcpServers"]["spendguard"]["command"].endswith("spendguard"))
    ck("the PRE-EXISTING server is preserved — registration is ADDITIVE, never clobbers", "symgrep" in cfg["mcpServers"])
    ck("unrelated top-level keys are preserved (whole-file write is safe)",
       cfg.get("numStartups") == 42 and cfg.get("theme") == "dark")
    ck("a backup was written (reversible)", pathlib.Path(str(cfgp) + "~").exists())

    print("-- idempotent --")
    mcp_server.install()
    cfg2 = json.loads(cfgp.read_text())
    ck("re-register is idempotent (exactly the two servers, no duplication)",
       set(cfg2["mcpServers"]) == {"symgrep", "spendguard"})

    print("-- reversible --")
    mcp_server.install(remove=True)
    cfg3 = json.loads(cfgp.read_text())
    ck("remove unregisters spendguard", "spendguard" not in cfg3["mcpServers"])
    ck("remove keeps the OTHER servers (only ours is dropped)", "symgrep" in cfg3["mcpServers"])
    rc = mcp_server.install(remove=True)
    ck("removing when already absent is a graceful no-op (rc 0, no crash)", rc == 0)

    print("-- parse-safety: an unreadable existing config is NOT silently replaced, and we say so --")
    cfgp.write_text("{ this is not json ]")
    rc_bad = mcp_server.install()
    ck("install REFUSES (rc 1) rather than falsely claiming success on an unparseable config", rc_bad == 1)
    ck("the unparseable config is left byte-for-byte intact, never clobbered to empty",
       cfgp.read_text() == "{ this is not json ]")
finally:
    pathlib.Path.home = _orig_home

print(("[OK]" if not fails else "[FAIL]") + " test_mcp_install: %d failure(s)" % len(fails))
sys.exit(1 if fails else 0)
