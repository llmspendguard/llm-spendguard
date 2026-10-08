"""The recurring 'codex logged out, re-login' TOAST was a FALSE positive: codex_exec.auth_status let a flaky non-zero
`codex login status` (which races the periodic OAuth token refresh that rewrites ~/.codex/auth.json) escalate to a
CONFIRMED logout (authed=False) → reliability.note_lane_auth_down → the macOS toast — every refresh, while the token
was valid. Fix: the TOKEN on disk is authoritative; an UNREADABLE (mid-refresh) token is INCONCLUSIVE (None), never a
confirmed logout. authed is False ONLY for a present-and-expired token or a genuinely ABSENT auth file. Offline — the
token state, the status subprocess, and auth.json presence are all controlled; zero spend."""
import os
import sys
import tempfile

os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"
os.environ["SPENDGUARD_NO_AUTOINSTALL"] = "1"
os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="sg-codexauth-")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from spendguard import codex_exec  # noqa: E402

fails = []
def ck(label, cond):
    print(f"  [{'OK' if cond else 'FAIL'}] {label}")
    if not cond:
        fails.append(label)

# no real backoff wait in-test (design does ONE confirming re-check on an unreadable token)
codex_exec._AUTH_RECHECK_BACKOFF_S = 0.0
codex_exec.time.sleep = lambda *_: None
codex_exec._bin = lambda: "/fake/codex"

class _R:
    def __init__(self, rc): self.returncode = rc; self.stdout = ""; self.stderr = ""
_status_rc = {"rc": 1}                                  # default: the flaky status subprocess returns NON-zero
codex_exec.subprocess.run = lambda *a, **k: _R(_status_rc["rc"])

def _set_token(v): codex_exec._token_unexpired = lambda *a, **k: v   # accepts margin= (auth_status passes margin=0)

def _run(token, rc=1, auth_present=True):
    _status_rc["rc"] = rc
    _set_token(token)
    home = tempfile.mkdtemp(prefix="sg-codexhome-")
    os.environ["CODEX_HOME"] = home
    if auth_present:
        open(os.path.join(home, "auth.json"), "w").write("{}")   # file EXISTS (content irrelevant; token stubbed)
    return codex_exec.auth_status(timeout=5).get("authed")

# ── THE false-toast case: flaky non-zero status but a VALID token → authed True, NOT False (no banner) ──
ck("valid token + flaky non-zero `login status` → authed True (the false-logout case is gone)", _run(True, rc=1) is True)
# ── a present-but-EXPIRED token → a real, definitive logout ──
ck("present-and-expired token → authed False (a real logout still reports)", _run(False, rc=1) is False)
# ── token UNREADABLE for the whole window + auth.json PRESENT (mid-refresh write) → INCONCLUSIVE, never False ──
ck("unreadable token + auth.json present (mid-refresh) → authed None (inconclusive, no banner)",
   _run(None, rc=1, auth_present=True) is None)
# ── token UNREADABLE + auth.json genuinely ABSENT → a real logout ──
ck("unreadable token + auth.json absent → authed False (real logout)",
   _run(None, rc=1, auth_present=False) is False)
# ── status exits 0 → authed True (fast path, no token read needed) ──
ck("`login status` exit 0 → authed True", _run(None, rc=0) is True)

print(f"\n{'[FAIL]' if fails else 'OK'} test_codex_auth_refresh_race: {len(fails)} failure(s)")
sys.exit(1 if fails else 0)
