"""claude-code lane auth hardening: a SINGLE `loggedIn: false` from `claude auth status` can be a transient flap while
the Claude OAuth token refreshes — it must be CONFIRMED by one re-check before reporting a logout, so a refresh blip
never false-fires the 'claude-code lane LOGGED OUT' banner (the same class the codex fix closed). A real logout stays
false on the re-check; True is authoritative on the first read; an unreadable loggedIn is inconclusive (None), never a
false logout. Offline: the `claude auth status` subprocess is scripted; no CLI, no spend."""
import json
import os
import sys
import tempfile

os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"
os.environ["SPENDGUARD_NO_AUTOINSTALL"] = "1"
os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="sg-cc-auth-")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from spendguard import subscription_exec as se  # noqa: E402

fails = []
def ck(label, cond):
    print(f"  [{'OK' if cond else 'FAIL'}] {label}")
    if not cond:
        fails.append(label)

class _P:
    def __init__(self, stdout): self.stdout = stdout; self.stderr = ""; self.returncode = 0

se._bin = lambda: "/usr/bin/true"
se.time.sleep = lambda *_: None                      # no real re-check backoff in-test

def _script(*stdouts):
    """subprocess.run stand-in yielding the given stdout strings in order; counts calls."""
    seq = list(stdouts); calls = {"n": 0}
    def _run(*a, **k):
        s = seq[calls["n"]] if calls["n"] < len(seq) else seq[-1]
        calls["n"] += 1
        return _P(s)
    return _run, calls

_T = json.dumps({"loggedIn": True})
_F = json.dumps({"loggedIn": False})
_BAD = "not json at all"
_MISSING = json.dumps({"other": 1})

se.subprocess.run, c1 = _script(_T)
ck("loggedIn true → authed True (authoritative on the first read, no re-check)", se.auth_status() == {"authed": True})
ck("...with exactly one status call", c1["n"] == 1)

se.subprocess.run, c2 = _script(_F, _T)
ck("loggedIn false then true → authed True (transient flap recovered via the re-check)", se.auth_status() == {"authed": True})
ck("...using one confirming re-check (two status calls)", c2["n"] == 2)

se.subprocess.run, _ = _script(_F, _F)
ck("loggedIn false on BOTH → authed False (a real logout still reports)", se.auth_status() == {"authed": False})

se.subprocess.run, _ = _script(_F, _BAD)
ck("loggedIn false then unreadable re-check → authed None (inconclusive, never the banner)", se.auth_status() == {"authed": None})

se.subprocess.run, c5 = _script(_BAD)
ck("unparseable first read → authed None (no re-check, no false logout)", se.auth_status() == {"authed": None})
ck("...one status call only", c5["n"] == 1)

se.subprocess.run, _ = _script(_MISSING)
ck("missing loggedIn field → authed None (inconclusive)", se.auth_status() == {"authed": None})

print(f"\n{'[FAIL]' if fails else 'OK'} test_claude_code_auth_recheck: {len(fails)} failure(s)")
sys.exit(1 if fails else 0)
