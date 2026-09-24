"""Guard — api ↔ MCP ↔ cli PARITY for the rate-admission / queue / reasoning-cut observability + config knobs.
ONE assembler (dispatch.admission_state) feeds BOTH the CLI (`spendguard dispatch`) and the MCP tool
(spendguard_dispatch_state), so the three surfaces cannot drift; the MCP spendguard_config tool exposes every
config_schema knob (incl. the new rate-admission / reasoning-economics ones) with its current value and NO secrets."""
import contextlib
import io
import json
import os
import sys
import tempfile

os.environ.setdefault("SPENDGUARD_TEST_ISOLATED", "1")
os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="spendguard-parity-")

from spendguard import cli, config_schema, dispatch, mcp_server

_fails = []
def ck(label, cond):
    if not cond:
        _fails.append(label)
    print(f"  [{'OK' if cond else 'FAIL'}] {label}")

# ── (1) the assembler + its two renderers agree (the parity core) ──
print("-- (1) admission_state is the ONE source; MCP + CLI render the same keys --")
st = dispatch.admission_state()
ck("assembler has all five sections", set(st) == {"manage_all", "governor", "learned_limits", "queue", "deadline_cancels"})
ck("queue depth includes PARKED (Step-4 backpressure is visible)", "parked" in (st.get("queue") or {}))

tool = mcp_server._TOOLS["spendguard_dispatch_state"][2]({})
ck("MCP spendguard_dispatch_state carries every assembler section", all(k in tool for k in st))

buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    rc = cli._dispatch(["dispatch", "--json"])
cli_data = json.loads(buf.getvalue())
ck("CLI `dispatch --json` exits 0 and emits the SAME sections (api/MCP/cli parity)",
   rc == 0 and set(cli_data) == set(st))

# ── (2) config parity: the MCP config tool exposes every NEW knob, with values, and NO secrets ──
print("-- (2) MCP spendguard_config exposes the new knobs (with current values) and never a secret --")
cfg = mcp_server._TOOLS["spendguard_config"][2]({})
keys = {k["key"] for k in cfg["knobs"]}
for knob in ("dispatch.manage_all", "dispatch.cooldown_cap_s", "advisor.queue_park_backoff_s",
             "advisor.queue_max_parks", "bulkgate.reasoning_out_estimate", "advisor.reasoning_deadline_floor_s"):
    ck(f"MCP config exposes {knob}", knob in keys)
ck("every listed knob carries a current value + a description", all("value" in k and k.get("desc") for k in cfg["knobs"]))
_secret_keys = {"%s.%s" % (s["section"], s["key"]) for s in config_schema.SETTINGS if s.get("secret")}
ck("no secret knob is exposed over MCP", not (_secret_keys & keys) and bool(_secret_keys))

# ── (3) both new tools are registered (discoverable via tools/list) ──
print("-- (3) the new capabilities are registered as MCP tools (discoverable) --")
ck("spendguard_dispatch_state + spendguard_config are registered",
   "spendguard_dispatch_state" in mcp_server._TOOLS and "spendguard_config" in mcp_server._TOOLS)

print(f"\n{'[FAIL]' if _fails else 'OK'} test_dispatch_parity: {len(_fails)} failure(s)")
sys.exit(1 if _fails else 0)
