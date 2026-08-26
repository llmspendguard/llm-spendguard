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
from spendguard import lanes, subscription_exec, codex_exec, antigravity_exec, zai_exec

fails = []
def ck(name, cond):
    print(("  [OK] " if cond else "  [FAIL] ") + name)
    if not cond:
        fails.append(name)


tmp = Path(tempfile.mkdtemp(prefix="lanes-artifacts-"))
lanes.CLAUDE_CREDS = tmp / "claude-creds.json"     # point artifact constants at controlled paths
lanes.CODEX_AUTH = tmp / "codex-auth.json"
lanes.GEMINI_OAUTH_TOKEN = tmp / "antigravity-oauth-token"   # current agy layout — BOTH artifacts must be controlled,
lanes.GEMINI_CREDS = tmp / "gemini-creds.json"               # else the host's real agy token leaks into 'no creds'

print("-- executor=api: nothing enabled, summary stays silent --")
os.environ["SPENDGUARD_ADVISOR_EXECUTOR"] = "api"
ck("no lanes enabled", not any(ln["enabled"] for ln in lanes.status()["lanes"]))
ck("summary is empty (nothing to nag about)", lanes.summary_lines() == [])

print("-- pool + missing CLIs: every enabled lane says exactly how to activate --")
os.environ["SPENDGUARD_ADVISOR_EXECUTOR"] = "pool"
subscription_exec._bin = lambda: None
codex_exec._bin = lambda: None
antigravity_exec._bin = lambda: None
zai_exec._key = lambda: None                        # key-based lane: no key → unconfigured
s = lanes.status()
byln = {ln["lane"]: ln for ln in s["lanes"]}
ck("every lane enabled under pool", all(ln["enabled"] for ln in s["lanes"]))
ck("zai lane is SURFACED alongside the CLI lanes (not skipped for lacking a binary)", "zai-coding" in byln)
ck("every unconfigured lane names an activation step", all(ln["activate"] for ln in s["lanes"]))
lines = lanes.summary_lines()
ck("summary shows inactive lanes + the API-fallback consequence",
   any("🔴 inactive" in l for l in lines) and any("fall back to the metered API" in l for l in lines))

print("-- CLI present, auth artifacts drive the verdict --")
subscription_exec._bin = lambda: "/fake/claude"
codex_exec._bin = lambda: "/fake/codex"
antigravity_exec._bin = lambda: "/fake/agy"
zai_exec._key = lambda: None                                        # key-based lane still unconfigured here
lanes._CLAUDE_KEYCHAIN_SERVICE = "spendguard-test-no-such-service"   # keychain lookup must MISS
s = {ln["lane"]: ln for ln in lanes.status()["lanes"]}
ck("claude: no artifact → missing + /login step",
   s["claude-code"]["auth"] == "missing" and "/login" in s["claude-code"]["activate"])
ck("codex: no auth.json → missing + sign-in step",
   s["codex"]["auth"] == "missing" and "ChatGPT" in s["codex"]["activate"])
ck("gemini: no creds → missing + Google sign-in step",
   s["gemini"]["auth"] == "missing" and "Google" in s["gemini"]["activate"])
ck("zai: key-based → no key makes it missing, independent of any _bin/creds",
   s["zai-coding"]["auth"] == "missing" and s["zai-coding"]["cli"] is None and bool(s["zai-coding"]["activate"]))
lanes.CODEX_AUTH.write_text("{}")
lanes.CLAUDE_CREDS.write_text("{}")
lanes.GEMINI_CREDS.write_text("{}")
zai_exec._key = lambda: "zai-test-key"                              # the plan key now resolves — the ONLY change for zai
s = {ln["lane"]: ln for ln in lanes.status()["lanes"]}
ck("auth artifacts present → all lanes ok, no activation steps",
   all(s[l]["auth"] == "ok" for l in ("claude-code", "codex", "gemini", "zai-coding"))
   and all(s[l]["activate"] is None for l in ("claude-code", "codex", "gemini", "zai-coding")))
ck("summary shows ready lanes", all("🟢 ready" in l for l in lanes.summary_lines()[1:-1]))
ck("key lane renders ready WITHOUT a binary path (key-based, not a CLI)",
   any("zai-coding" in l and "🟢 ready" in l and "/" not in l for l in lanes.summary_lines()))

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
antigravity_exec.run_prompt = lambda p, system=None, model=None, timeout=None: {"error": "no agy in test"}
zai_exec.run_prompt = lambda p, system=None, model=None, timeout=None: {"text": "OK", "in_tok": 3, "out_tok": 1, "latency": 0.4, "error": None}
res = {r["lane"]: r for r in lanes.probe()}
ck("live lane reports ok", res["claude-code"]["ok"] and res["claude-code"]["text"] == "OK")
ck("probe pins an explicit cheap tier (immune to a stale CLI default model)",
   probe_seen["claude_model"] == "haiku")
ck("dead lane reports its error", not res["codex"]["ok"] and "window" in res["codex"]["error"])
ck("key lane (zai) probes through run_prompt like the CLI lanes", res["zai-coding"]["ok"])
s2 = {ln["lane"]: ln for ln in lanes.status()["lanes"]}
ck("a successful probe persists as definitive auth evidence (macOS keychain can't prove login)",
   s2["claude-code"]["auth"] == "ok")
os.environ["SPENDGUARD_ADVISOR_EXECUTOR"] = "claude-code"
res = {r["lane"]: r for r in lanes.probe()}
ck("single-lane executor probes only its lane", res["codex"].get("skipped") and res["claude-code"]["ok"])

del os.environ["SPENDGUARD_ADVISOR_EXECUTOR"]
print(f"\n{'[FAIL]' if fails else 'OK'} test_lanes: {len(fails)} failure(s)")
sys.exit(1 if fails else 0)
