"""Positive lane-AUTH detection + surfacing — the fix for the SILENT logout the user hit: a $0 lane that is logged
out kept falling back to the metered API with NO clear message (a background job's stderr goes to a log file, and an
empty-but-no-error reply was never surfaced at all). Now each lane's OWN status command is consulted (a STRUCTURAL
loggedIn bool / process exit code, never a regex on error prose); a CONFIRMED logout is classed reason 'auth', an
unhedged actionable line is printed with the exact re-login command, and a PERSISTENT alert rides every receipt's
banner until the lane's next successful call clears it. Offline (stubbed subprocess), zero spend."""
import os
import sys
import tempfile

if not os.environ.get("SPENDGUARD_TEST_ISOLATED"):
    os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"
    os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="spendguard-laneauth-")
    os.execv(sys.executable, [sys.executable] + sys.argv)

import json
import types
from spendguard import subscription_exec as se
from spendguard import codex_exec as cx
from spendguard import lanes, reliability

fails = []


def ck(name, cond):
    print(("  [OK] " if cond else "  [FAIL] ") + name)
    if not cond:
        fails.append(name)


def _fake_run_json(payload, returncode=0):
    def _run(cmd, capture_output=None, text=None, timeout=None, env=None):
        return types.SimpleNamespace(returncode=returncode, stdout=json.dumps(payload), stderr="")
    return _run


def _fake_run_exit(returncode, out="", err=""):
    def _run(cmd, capture_output=None, text=None, timeout=None, env=None):
        return types.SimpleNamespace(returncode=returncode, stdout=out, stderr=err)
    return _run


# ── 1. subscription_exec.auth_status: reads the STRUCTURAL `loggedIn` bool from `claude auth status` JSON ──
se._bin = lambda: "/usr/local/bin/claude"
se.subprocess.run = _fake_run_json({"loggedIn": False, "authMethod": "claude.ai"})
ck("claude logged OUT → authed False (positive detection, not a guess)", se.auth_status().get("authed") is False)
se.subprocess.run = _fake_run_json({"loggedIn": True, "subscriptionType": "max"})
ck("claude logged in → authed True", se.auth_status().get("authed") is True)
se.subprocess.run = _fake_run_exit(0, out="not json")
ck("unparseable status → authed None (can't-check is NEVER a false logout)", se.auth_status().get("authed") is None)
def _boom(*a, **k):
    raise RuntimeError("cli crashed")
se.subprocess.run = _boom
ck("status command crash → authed None, never raises", se.auth_status().get("authed") is None)
se._bin = lambda: None
ck("claude CLI absent → authed None", se.auth_status().get("authed") is None)

# ── 2. codex_exec.auth_status: the process EXIT CODE is the signal (0 = signed in), never the printed prose ──
cx._bin = lambda: "/usr/local/bin/codex"
cx.subprocess.run = _fake_run_exit(0, out="Logged in using ChatGPT")
ck("codex exit 0 → authed True", cx.auth_status().get("authed") is True)
cx.subprocess.run = _fake_run_exit(1, err="Not logged in")
ck("codex exit non-zero → authed False (logged out)", cx.auth_status().get("authed") is False)

# ── 3. lanes.lane_auth_status: dispatch to the exec + attach the registry's authored relogin_cmd ──
lanes._auth_status_cache.clear()
se._bin = lambda: "/usr/local/bin/claude"
se.auth_status = lambda *a, **k: {"authed": False}
a = lanes.lane_auth_status("claude-code")
ck("lane dispatch: claude-code logged out → authed False + cmd 'claude auth login' (from the registry, one place)",
   a.get("authed") is False and a.get("cmd") == "claude auth login")
ck("unknown lane → authed None, cmd '' (never cached, so the cache stays registry-bounded)",
   lanes.lane_auth_status("nope-not-a-lane") == {"authed": None, "cmd": ""})
ck("a key lane with no cheap status command (zai-coding) → authed None (never a false logout claim)",
   lanes.lane_auth_status("zai-coding").get("authed") is None)

# ── 4. surfacing chain: a confirmed logout writes a PERSISTENT alert that the receipt banner renders ──
reliability._notify_macos = lambda *a, **k: None          # don't fire a real desktop notification in the suite
reliability.note_lane_auth_down("claude-code", "claude auth login")
reds = {r["resource"]: r for r in reliability.health_reds()}
ck("logout recorded as an unreachable lane (rides every receipt for $0, no new sweep)", "claude-code" in reds)
ck("the alert carries the exact re-login command", reds.get("claude-code", {}).get("command") == "claude auth login")
alert = reliability.health_alert() or ""
ck("receipt banner names the lane + the one-line fix", "claude-code" in alert and "claude auth login" in alert)

# ── 5. a generic 'down' (both lane AND fallback failed) must NOT clobber the more-specific confirmed-logout row ──
reliability.note_lane_down("claude-code", "some generic executor error")
reds2 = {r["resource"]: r for r in reliability.health_reds()}
ck("note_lane_down does not downgrade a confirmed logout (auth diagnosis + command preserved)",
   reds2.get("claude-code", {}).get("command") == "claude auth login")

# ── 6. self-heal: the lane's next SUCCESSFUL call clears the alert (re-login → banner clears on its own) ──
reliability.note_lane_ok("claude-code")
reds3 = {r["resource"]: r for r in reliability.health_reds()}
ck("a served call clears the logout alert (re-login self-heals the banner)", "claude-code" not in reds3)

print(("[OK]" if not fails else "[FAIL]") + " lane auth detection: %d failure(s)" % len(fails))
sys.exit(1 if fails else 0)
