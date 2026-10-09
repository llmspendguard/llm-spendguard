"""codex_exec.auth_status must NOT report a logout when ~/.codex/auth.json is momentarily ABSENT or UNREADABLE during
the CLI's periodic token refresh (a non-atomic unlink+write). Measured 2026-10-09: under heavy concurrent codex use
and at the hourly refresh, the 'codex lane LOGGED OUT' toast fired repeatedly while the token was in fact valid for
~8 more days — because an ABSENT auth.json (refresh rewrite in flight) escalated straight to {'authed': False}, which
is the sole trigger for reliability.note_lane_auth_down's toast (adapters.py). A real logout (present-but-EXPIRED
token, or a PERSISTENTLY absent file) must still surface. Offline, no network, no real codex binary, no LLM."""
import os
import sys
import tempfile

os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="sg-codexauth-")
os.environ.setdefault("SPENDGUARD_TEST_ISOLATED", "1")
os.environ.setdefault("SPENDGUARD_NO_AUTOINSTALL", "1")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from spendguard import codex_exec  # noqa: E402

fails = []
def ck(label, cond):
    print(f"  [{'OK' if cond else 'FAIL'}] {label}")
    if not cond:
        fails.append(label)


class _R:                                   # a fake subprocess result: a NON-ZERO status forces the token-based tail
    def __init__(self, rc):
        self.returncode = rc
        self.stdout = self.stderr = ""


# isolate CODEX_HOME + make the confirm window instant for the test
_codex_home = tempfile.mkdtemp(prefix="codex-home-")
os.environ["CODEX_HOME"] = _codex_home
_auth = os.path.join(_codex_home, "auth.json")
codex_exec._AUTH_RECHECK_BACKOFF_S = 0.0    # no real sleeping in the confirm loop
codex_exec._ABSENT_CONFIRM_TRIES = 3
codex_exec._bin = lambda: "/usr/bin/true"   # a present (fake) binary so auth_status reaches the status call
codex_exec.subprocess.run = lambda *a, **k: _R(1)   # status + re-check both NON-ZERO → drive the token-based tail


def _set_token(result_seq):
    """Install a _token_unexpired stub that yields the given results in order (then repeats the last)."""
    seq = list(result_seq)
    def _stub(now=None, margin=None):
        return seq[0] if len(seq) == 1 else (seq.pop(0) if seq else None)
    codex_exec._token_unexpired = _stub


# ── 1. PERSISTENTLY ABSENT auth.json (real `codex logout`) → authed False ──
if os.path.exists(_auth):
    os.remove(_auth)
_set_token([None])                          # token never readable (file absent)
ck("persistently-absent auth.json → authed False (real logout)",
   codex_exec.auth_status(timeout=1).get("authed") is False)

# ── 2. ABSENT FLICKER but a valid token reappears mid-confirm → authed True (NOT False) ──
_set_token([None, None, True])              # unreadable twice (rewrite in flight), then the new valid token lands
ck("absent/unreadable flicker then valid token → authed True (no false logout)",
   codex_exec.auth_status(timeout=1).get("authed") is True)

# ── 3. PRESENT but genuinely EXPIRED token → authed False (a real logout still surfaces) ──
open(_auth, "w").write("{}")                # file present
_set_token([False])                         # present + expired
ck("present + expired token → authed False (real logout still surfaces)",
   codex_exec.auth_status(timeout=1).get("authed") is False)

# ── 4. PRESENT-but-unreadable the whole window (pure refresh rewrite) → inconclusive None (NEVER a toast) ──
open(_auth, "w").write("garbage-not-a-jwt")  # present but never yields a readable token
_set_token([None])                          # unreadable every check, file present throughout
ck("present-but-unreadable whole window → authed None (inconclusive, no toast)",
   codex_exec.auth_status(timeout=1).get("authed") is None)

print(f"\n{'[FAIL]' if fails else 'OK'} test_codex_auth_absent_flicker: {len(fails)} failure(s)")
sys.exit(1 if fails else 0)
