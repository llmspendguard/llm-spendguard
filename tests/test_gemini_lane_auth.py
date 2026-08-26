"""The gemini lane's login detection must not report a logged-in, working agy as '🔴 inactive — install the CLI'.

The bug this pins: _gemini_auth() checked ONE hardcoded path (~/.gemini/oauth_creds.json) for the agy OAuth
login. agy actually stores the token at ~/.gemini/antigravity-cli/antigravity-oauth-token (the path drifts
across CLI versions), so the check returned 'missing' for an installed, logged-in, working lane — and the
readout told the operator (and every repo reading it) to install a CLI that was already there. Fixed to accept
ANY known artifact and to honour a successful probe (like _claude_auth). Pins:

  (a) the CURRENT agy layout (antigravity-oauth-token present, legacy oauth_creds.json ABSENT) reads 'ok' — the
      exact regression;
  (b) a successful probe is definitive even with no artifact file (matches the claude lane);
  (c) a FAILED probe (e.g. quota exhausted) does NOT downgrade a logged-in lane — quota is a runtime state, not a
      login failure, and the work still flows via the metered API;
  (d) genuinely-absent login (no artifact, no ok probe) still reads 'missing'.

Offline: the artifact paths and the probe cache are redirected into an isolated home; no agy, no network.
"""
import os
import sys
import tempfile
from pathlib import Path

os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="sg-gemauth-")
os.environ.setdefault("SPENDGUARD_TEST_ISOLATED", "1")
os.environ.setdefault("SPENDGUARD_NO_AUTOINSTALL", "1")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from spendguard import lanes                                                           # noqa: E402

fails = []


def ck(name, cond):
    ok = bool(cond)
    print(("  [OK] " if ok else "  [FAIL] ") + name)
    if not ok:
        fails.append(name)


# Redirect BOTH artifact paths into the isolated home so the test controls existence.
_root = Path(os.environ["SPENDGUARD_HOME"])
_token = _root / "gemini" / "antigravity-cli" / "antigravity-oauth-token"   # current agy layout
_legacy = _root / "gemini" / "oauth_creds.json"                             # legacy layout
lanes.GEMINI_OAUTH_TOKEN = _token
lanes.GEMINI_CREDS = _legacy


def _reset():
    for p in (_token, _legacy):
        if p.exists():
            p.unlink()
    pc = lanes._probe_cache_path()
    if pc.exists():
        pc.unlink()


print("-- (a) CURRENT agy layout: token present, legacy creds ABSENT → 'ok' (the exact regression) --")
_reset()
_token.parent.mkdir(parents=True, exist_ok=True)
_token.write_text("oauth-token-bytes")
ck("agy's real token artifact is accepted as logged-in", lanes._gemini_auth() == "ok")

print("\n-- (b) a successful probe is definitive even with no artifact file --")
_reset()
lanes._record_probe("gemini", True)
ck("a recorded successful probe → 'ok' (matches _claude_auth)", lanes._gemini_auth() == "ok")

print("\n-- (c) a FAILED probe does NOT downgrade a logged-in lane (quota != login failure) --")
_reset()
_token.parent.mkdir(parents=True, exist_ok=True)
_token.write_text("oauth-token-bytes")
lanes._record_probe("gemini", False)                    # e.g. 'Individual quota reached — resets in 97h'
ck("quota-failed probe + present login artifact still reads 'ok'", lanes._gemini_auth() == "ok")

print("\n-- (d) genuinely absent login still reads 'missing' --")
_reset()
ck("no artifact and no ok probe → 'missing'", lanes._gemini_auth() == "missing")

print(f"\n{'[FAIL]' if fails else 'OK'} test_gemini_lane_auth: {len(fails)} failure(s)")
sys.exit(1 if fails else 0)
