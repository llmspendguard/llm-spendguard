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
from spendguard import lanes, subscription_exec, codex_exec, antigravity_exec, zai_exec, kimi_exec
from spendguard import lane_registry   # the ONE lane data table (a submodule import, like lane_catalog elsewhere)

fails = []
def ck(name, cond):
    print(("  [OK] " if cond else "  [FAIL] ") + name)
    if not cond:
        fails.append(name)


tmp = Path(tempfile.mkdtemp(prefix="lanes-artifacts-"))
# DATA-DRIVEN isolation: repoint each lane's login artifact(s) in the ONE registry at controlled temp paths, so no
# suite reads the host's real login and the host's real CLI (kimi IS installed here) can't leak into an offline check.
_CLAUDE = tmp / "claude-creds.json"
_CODEX = tmp / "codex-auth.json"
_GEMINI = tmp / "gemini-creds.json"
_KIMI = tmp / "kimi-creds"
lane_registry.lane_spec("claude-code")["creds"] = (_CLAUDE,)
lane_registry.lane_spec("codex")["creds"] = (_CODEX,)
lane_registry.lane_spec("gemini")["creds"] = (tmp / "antigravity-oauth-token", _GEMINI)   # BOTH agy artifacts controlled
lane_registry.lane_spec("kimi-code")["creds"] = (_KIMI,)

print("-- executor=api: nothing enabled, summary stays silent --")
os.environ["SPENDGUARD_ADVISOR_EXECUTOR"] = "api"
ck("no lanes enabled", not any(ln["enabled"] for ln in lanes.lanes_status()["lanes"]))
ck("summary is empty (nothing to nag about)", lanes.lane_summary_lines() == [])

print("-- pool + missing CLIs: every enabled lane says exactly how to activate --")
os.environ["SPENDGUARD_ADVISOR_EXECUTOR"] = "pool"
subscription_exec._bin = lambda: None
codex_exec._bin = lambda: None
antigravity_exec._bin = lambda: None
kimi_exec._bin = lambda: None                       # kimi CLI is installed on this dev host → stub it missing here
zai_exec._key = lambda: None                        # key-based lane: no key → unconfigured
s = lanes.lanes_status()
byln = {ln["lane"]: ln for ln in s["lanes"]}
ck("every lane enabled under pool", all(ln["enabled"] for ln in s["lanes"]))
ck("zai lane is SURFACED alongside the CLI lanes (not skipped for lacking a binary)", "zai-coding" in byln)
ck("every unconfigured lane names an activation step", all(ln["activate"] for ln in s["lanes"]))
lines = lanes.lane_summary_lines()
ck("summary shows inactive lanes + the API-fallback consequence",
   any("🔴 inactive" in l for l in lines) and any("fall back to the metered API" in l for l in lines))

print("-- CLI present, auth artifacts drive the verdict --")
subscription_exec._bin = lambda: "/fake/claude"
codex_exec._bin = lambda: "/fake/codex"
antigravity_exec._bin = lambda: "/fake/agy"
kimi_exec._bin = lambda: "/fake/kimi"
zai_exec._key = lambda: None                                        # key-based lane still unconfigured here
lane_registry.lane_spec("claude-code")["keychain"] = "spendguard-test-no-such-service"   # keychain lookup must MISS
s = {ln["lane"]: ln for ln in lanes.lanes_status()["lanes"]}
ck("claude: no artifact → missing + /login step",
   s["claude-code"]["auth"] == "missing" and "/login" in s["claude-code"]["activate"])
ck("codex: no auth.json → missing + sign-in step",
   s["codex"]["auth"] == "missing" and "ChatGPT" in s["codex"]["activate"])
ck("gemini: no creds → missing + Google sign-in step",
   s["gemini"]["auth"] == "missing" and "Google" in s["gemini"]["activate"])
ck("zai: key-based → no key makes it missing, independent of any _bin/creds",
   s["zai-coding"]["auth"] == "missing" and s["zai-coding"]["cli"] is None and bool(s["zai-coding"]["activate"]))
ck("kimi: CLI present but no login artifact → missing + /login step",
   s["kimi-code"]["auth"] == "missing" and "/login" in s["kimi-code"]["activate"])
_CODEX.write_text("{}")
_CLAUDE.write_text("{}")
_GEMINI.write_text("{}")
_KIMI.write_text("{}")                                             # kimi login artifact now present
zai_exec._key = lambda: "zai-test-key"                              # the plan key now resolves — the ONLY change for zai
s = {ln["lane"]: ln for ln in lanes.lanes_status()["lanes"]}
ck("auth artifacts present → all lanes ok, no activation steps",
   all(s[l]["auth"] == "ok" for l in ("claude-code", "codex", "gemini", "zai-coding", "kimi-code"))
   and all(s[l]["activate"] is None for l in ("claude-code", "codex", "gemini", "zai-coding", "kimi-code")))
ck("summary shows ready lanes", all("🟢 ready" in l for l in lanes.lane_summary_lines()[1:-1]))
ck("key lane renders ready WITHOUT a binary path (key-based, not a CLI)",
   any("zai-coding" in l and "🟢 ready" in l and "/" not in l for l in lanes.lane_summary_lines()))

print("-- keychain-only is NEVER 'ok' (desktop-app item ≠ CLI login) --")
_CLAUDE.unlink()
real_run = lanes.subprocess.run
lanes.subprocess.run = lambda *a, **k: type("R", (), {"returncode": 0})()   # keychain item "exists"
if sys.platform == "darwin":
    ck("keychain hit without creds file → unknown, probe suggested",
       {ln["lane"]: ln for ln in lanes.lanes_status()["lanes"]}["claude-code"]["auth"] == "unknown")
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
kimi_exec.run_prompt = lambda p, system=None, model=None, timeout=None, reasoning=None, max_tokens=None: {"text": "OK", "in_tok": 4, "out_tok": 1, "latency": 0.5, "error": None}
res = {r["lane"]: r for r in lanes.probe()}
ck("live lane reports ok", res["claude-code"]["ok"] and res["claude-code"]["text"] == "OK")
ck("probe pins an explicit cheap tier (immune to a stale CLI default model)",
   probe_seen["claude_model"] == "haiku")
ck("dead lane reports its error", not res["codex"]["ok"] and "window" in res["codex"]["error"])
ck("key lane (zai) probes through run_prompt like the CLI lanes", res["zai-coding"]["ok"])
ck("kimi lane probes through run_prompt like the other CLI lanes", res["kimi-code"]["ok"])
s2 = {ln["lane"]: ln for ln in lanes.lanes_status()["lanes"]}
ck("a successful probe persists as definitive auth evidence (macOS keychain can't prove login)",
   s2["claude-code"]["auth"] == "ok")
os.environ["SPENDGUARD_ADVISOR_EXECUTOR"] = "claude-code"
res = {r["lane"]: r for r in lanes.probe()}
ck("single-lane executor probes only its lane", res["codex"].get("skipped") and res["claude-code"]["ok"])

print("-- overage_nudge_line: fires ONLY when a KNOWN lane is at/below the display-warn level --")
from spendguard import config as _cfg


def _snap(rows):
    _cfg.save_state(lanes._HEADROOM_SNAPSHOT, {"asof": 1.0, "rows": rows}, loud=False)


# a provider that exposes NO quota surface is 'unknown' (known=False) — never counted as 'low', no false nudge
_snap([{"lane": "codex", "provider": "openai", "remaining_pct": None, "buckets": None, "known": False}])
ck("unknown-quota lane never nudges", lanes.overage_nudge_line(do_fetch=False) is None)
# a KNOWN lane comfortably above the warn level → silent
_snap([{"lane": "claude-code", "provider": "anthropic", "remaining_pct": lanes._QUOTA_WARN_PCT + 50,
        "buckets": [{}], "known": True}])
ck("a lane above the warn level is silent", lanes.overage_nudge_line(do_fetch=False) is None)
# a KNOWN lane at 0% (exhausted → overage) → a non-empty nudge string, and the low lane is named in it
_snap([{"lane": "claude-code", "provider": "anthropic", "remaining_pct": 0, "buckets": [{}], "known": True}])
_n = lanes.overage_nudge_line(do_fetch=False)
ck("an exhausted KNOWN lane produces a nudge string", isinstance(_n, str) and bool(_n.strip()))
ck("the nudge surfaces the low lane row it was given", _n is not None and "claude-code" in _n)

del os.environ["SPENDGUARD_ADVISOR_EXECUTOR"]
print(f"\n{'[FAIL]' if fails else 'OK'} test_lanes: {len(fails)} failure(s)")
sys.exit(1 if fails else 0)
