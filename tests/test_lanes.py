"""Subscription-lane ACTIVATION surface (lanes.py) — any user who sets advisor.executor to a plan lane must
be TOLD what activates it (init + doctor print this; `spendguard lanes --probe` verifies live). Key honesty
rule under test: a macOS keychain item alone reads 'unknown', never 'ok' — it can belong to the desktop app
while the CLI is logged out (the live 2026-07-16 lesson). Offline: CLIs + auth artifacts stubbed.
"""
import os, sys, tempfile
if not os.environ.get("SPENDGUARD_TEST_ISOLATED"):
    os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"
    os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="spendguard-lanes-")
    os.execv(sys.executable, [sys.executable] + sys.argv)

from pathlib import Path
from spendguard import lanes, subscription_exec, codex_exec

fails = []
def ck(name, cond):
    print(("  [OK] " if cond else "  [FAIL] ") + name)
    if not cond:
        fails.append(name)


tmp = Path(tempfile.mkdtemp(prefix="lanes-artifacts-"))
lanes.CLAUDE_CREDS = tmp / "claude-creds.json"     # point artifact constants at controlled paths
lanes.CODEX_AUTH = tmp / "codex-auth.json"

print("-- executor=api: nothing enabled, summary stays silent --")
os.environ["SPENDGUARD_ADVISOR_EXECUTOR"] = "api"
ck("no lanes enabled", not any(ln["enabled"] for ln in lanes.status()["lanes"]))
ck("summary is empty (nothing to nag about)", lanes.summary_lines() == [])

print("-- pool + missing CLIs: every enabled lane says exactly how to activate --")
os.environ["SPENDGUARD_ADVISOR_EXECUTOR"] = "pool"
subscription_exec._bin = lambda: None
codex_exec._bin = lambda: None
s = lanes.status()
ck("both lanes enabled under pool", all(ln["enabled"] for ln in s["lanes"]))
ck("missing CLI → install step named", all("install the" in (ln["activate"] or "") for ln in s["lanes"]))
lines = lanes.summary_lines()
ck("summary shows inactive lanes + the API-fallback consequence",
   any("🔴 inactive" in l for l in lines) and any("fall back to the metered API" in l for l in lines))

print("-- CLI present, auth artifacts drive the verdict --")
subscription_exec._bin = lambda: "/fake/claude"
codex_exec._bin = lambda: "/fake/codex"
lanes._CLAUDE_KEYCHAIN_SERVICE = "spendguard-test-no-such-service"   # keychain lookup must MISS
s = {ln["lane"]: ln for ln in lanes.status()["lanes"]}
ck("claude: no artifact → missing + /login step",
   s["claude-code"]["auth"] == "missing" and "/login" in s["claude-code"]["activate"])
ck("codex: no auth.json → missing + sign-in step",
   s["codex"]["auth"] == "missing" and "ChatGPT" in s["codex"]["activate"])
lanes.CODEX_AUTH.write_text("{}")
lanes.CLAUDE_CREDS.write_text("{}")
s = {ln["lane"]: ln for ln in lanes.status()["lanes"]}
ck("auth files present → both ok, no activation steps",
   s["claude-code"]["auth"] == "ok" and s["codex"]["auth"] == "ok"
   and s["claude-code"]["activate"] is None and s["codex"]["activate"] is None)
ck("summary shows ready lanes", all("🟢 ready" in l for l in lanes.summary_lines()[1:-1]))

print("-- keychain-only is NEVER 'ok' (desktop-app item ≠ CLI login) --")
lanes.CLAUDE_CREDS.unlink()
real_run = lanes.subprocess.run
lanes.subprocess.run = lambda *a, **k: type("R", (), {"returncode": 0})()   # keychain item "exists"
if sys.platform == "darwin":
    ck("keychain hit without creds file → unknown, probe suggested",
       {ln["lane"]: ln for ln in lanes.status()["lanes"]}["claude-code"]["auth"] == "unknown")
else:
    print("  (skip: keychain check is darwin-only)")
lanes.subprocess.run = real_run

print("-- probe: routes each enabled lane through its CLI; disabled lanes skipped --")
probe_seen = {}
def _claude_probe(p, system=None, model=None, timeout=None):
    probe_seen["claude_model"] = model
    return {"text": "OK", "in_tok": 5, "out_tok": 2, "latency": 1.2, "error": None}
subscription_exec.run_prompt = _claude_probe
codex_exec.run_prompt = lambda p, system=None, model=None, timeout=None: {"error": "plan window exhausted"}
res = {r["lane"]: r for r in lanes.probe()}
ck("live lane reports ok", res["claude-code"]["ok"] and res["claude-code"]["text"] == "OK")
ck("probe pins an explicit cheap tier (immune to a stale CLI default model)",
   probe_seen["claude_model"] == "haiku")
ck("dead lane reports its error", not res["codex"]["ok"] and "window" in res["codex"]["error"])
s2 = {ln["lane"]: ln for ln in lanes.status()["lanes"]}
ck("a successful probe persists as definitive auth evidence (macOS keychain can't prove login)",
   s2["claude-code"]["auth"] == "ok")
os.environ["SPENDGUARD_ADVISOR_EXECUTOR"] = "claude-code"
res = {r["lane"]: r for r in lanes.probe()}
ck("single-lane executor probes only its lane", res["codex"].get("skipped") and res["claude-code"]["ok"])

del os.environ["SPENDGUARD_ADVISOR_EXECUTOR"]
print(f"\n{'[FAIL]' if fails else 'OK'} test_lanes: {len(fails)} failure(s)")
sys.exit(1 if fails else 0)
