"""The codex lane must stay usable while its token is valid: a non-zero `codex login status` (the status subprocess
racing a concurrent token refresh) is NOT a logout — the token's own exp is authoritative — and a positive auth
retracts a stale 'logged out' banner. Without this, a valid 10-day token still produced hourly false re-login toasts
and phantom metered fallback. Offline: subprocess, exec and the token reader are monkeypatched; no codex CLI, no spend."""
import base64
import json
import os
import sys
import tempfile
import time

os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="sg-auth-robust-")
os.environ.setdefault("SPENDGUARD_TEST_ISOLATED", "1")
os.environ.setdefault("SPENDGUARD_NO_AUTOINSTALL", "1")
os.environ["OPENAI_API_KEY"] = "sk-test-auth-offline"
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from spendguard import codex_exec, lanes, reliability  # noqa: E402

fails = []


def ck(name, ok):
    print(("  ok   " if ok else "  FAIL ") + name)
    return [] if ok else [name]


class _Proc:
    def __init__(self, rc):
        self.returncode = rc
        self.stdout = ""
        self.stderr = ""


def _scripted_run(return_codes):
    """A subprocess.run stand-in yielding the given returncodes in order; counts calls."""
    seq = list(return_codes)
    calls = {"n": 0}

    def _run(*a, **k):
        rc = seq[calls["n"]] if calls["n"] < len(seq) else seq[-1]
        calls["n"] += 1
        return _Proc(rc)

    return _run, calls


# --- (A) token-exp is authoritative: a non-zero status with a VALID token is NOT a logout ----------------------
_orig_run, _orig_bin, _orig_sleep = codex_exec.subprocess.run, codex_exec._bin, codex_exec.time.sleep
_orig_tok = codex_exec._token_unexpired
codex_exec._bin = lambda: "/usr/bin/true"              # a resolvable exe so auth_status runs the (patched) subprocess
codex_exec.time.sleep = lambda *_: None                # don't actually wait the re-check backoff in a test
try:
    run_ok, _ = _scripted_run([0])
    codex_exec.subprocess.run = run_ok
    codex_exec._token_unexpired = lambda *a, **k: None   # a clean status never consults the token
    fails += ck("a clean status (rc 0) is authed with no token check", codex_exec.auth_status() == {"authed": True})

    run_valid, calls_v = _scripted_run([1, 1])           # status would say down on every call...
    codex_exec.subprocess.run = run_valid
    codex_exec._token_unexpired = lambda *a, **k: True   # ...but the token on disk is VALID → authoritative
    fails += ck("a non-zero status with a VALID token is NOT a logout (the hourly false-toast fix)",
                codex_exec.auth_status() == {"authed": True})
    fails += ck("...and it short-circuits BEFORE any re-check (one status call only)", calls_v["n"] == 1)

    run_exp, _ = _scripted_run([1, 1])
    codex_exec.subprocess.run = run_exp
    codex_exec._token_unexpired = lambda *a, **k: False  # token genuinely expired → a real logout
    fails += ck("a non-zero status with an EXPIRED token is reported authed=False",
                codex_exec.auth_status() == {"authed": False})

    run_transient, calls_t = _scripted_run([1, 0])       # token unreadable → fall back to the confirming re-check
    codex_exec.subprocess.run = run_transient
    codex_exec._token_unexpired = lambda *a, **k: None
    fails += ck("token unreadable + transient (1 then 0) recovers to authed via the re-check",
                codex_exec.auth_status() == {"authed": True})
    fails += ck("...using exactly one confirming re-check (two status calls)", calls_t["n"] == 2)

    # token unreadable + status down on BOTH calls: the verdict now turns on whether auth.json is a refresh write in
    # flight (present-but-unreadable → inconclusive, NO toast) or a genuine logout (absent → False). This is the fix
    # for the recurring hourly banner — an unreadable token is never escalated to a confirmed logout.
    _ah = tempfile.mkdtemp(prefix="codex-authrace-")
    _prev_ch = os.environ.get("CODEX_HOME")
    os.environ["CODEX_HOME"] = _ah
    try:
        codex_exec._token_unexpired = lambda *a, **k: None
        with open(os.path.join(_ah, "auth.json"), "w") as _fh:
            _fh.write("{}")                                  # auth.json PRESENT but token unreadable → mid-refresh write
        run_mid, _ = _scripted_run([1, 1])
        codex_exec.subprocess.run = run_mid
        fails += ck("unreadable token + status down + auth.json PRESENT (mid-refresh) → None (inconclusive, no toast)",
                    codex_exec.auth_status() == {"authed": None})
        os.remove(os.path.join(_ah, "auth.json"))            # auth.json ABSENT → a real logout
        run_gone, _ = _scripted_run([1, 1])
        codex_exec.subprocess.run = run_gone
        fails += ck("unreadable token + status down + auth.json ABSENT → authed False (a real logout still reports)",
                    codex_exec.auth_status() == {"authed": False})
    finally:
        if _prev_ch is None:
            os.environ.pop("CODEX_HOME", None)
        else:
            os.environ["CODEX_HOME"] = _prev_ch
finally:
    codex_exec.subprocess.run, codex_exec._bin, codex_exec.time.sleep = _orig_run, _orig_bin, _orig_sleep
    codex_exec._token_unexpired = _orig_tok


# --- (B) _token_unexpired decodes the JWT exp honestly (the authoritative signal itself) -----------------------
def _jwt(exp):
    payload = base64.urlsafe_b64encode(json.dumps({"exp": exp}).encode()).rstrip(b"=").decode()
    return "h." + payload + ".s"


_codex_home = tempfile.mkdtemp(prefix="codex-home-")
_orig_codex_home = os.environ.get("CODEX_HOME")
os.environ["CODEX_HOME"] = _codex_home
try:
    with open(os.path.join(_codex_home, "auth.json"), "w") as fh:
        json.dump({"tokens": {"access_token": _jwt(int(time.time()) + 10 * 86400)}}, fh)
    fails += ck("a token 10 days from exp reads unexpired (True)", codex_exec._token_unexpired() is True)

    with open(os.path.join(_codex_home, "auth.json"), "w") as fh:
        json.dump({"tokens": {"access_token": _jwt(int(time.time()) - 3600)}}, fh)
    fails += ck("a token past exp reads expired (False)", codex_exec._token_unexpired() is False)

    os.remove(os.path.join(_codex_home, "auth.json"))
    fails += ck("no auth.json reads inconclusive (None — never a false logout)", codex_exec._token_unexpired() is None)
finally:
    if _orig_codex_home is None:
        os.environ.pop("CODEX_HOME", None)
    else:
        os.environ["CODEX_HOME"] = _orig_codex_home


# --- (C) a positive auth retracts a stale 'logged out' banner (self-heal, no served call needed) ---------------
reliability.note_lane_auth_down("codex", "codex login")      # seed the persistent event-auth down row
_row = reliability._health_db().execute(
    "SELECT reachable, source FROM lane_health WHERE resource='codex'").fetchone()
fails += ck("precondition: codex banner is down (reachable=0, event-auth)", _row and _row[0] == 0 and _row[1] == "event-auth")

_orig_auth = codex_exec.auth_status
codex_exec.auth_status = lambda *a, **k: {"authed": True}     # the lane's exec now reports logged-in
try:
    out = lanes.lane_auth_status("codex", ttl=0)             # ttl=0 bypasses the brief status cache
    fails += ck("lane_auth_status reports authed=True", out.get("authed") is True)
    _row2 = reliability._health_db().execute(
        "SELECT reachable, source FROM lane_health WHERE resource='codex'").fetchone()
    fails += ck("the stale banner self-healed on positive auth (reachable=1, event-recovered)",
                _row2 and _row2[0] == 1 and _row2[1] == "event-recovered")
finally:
    codex_exec.auth_status = _orig_auth

print(("\n[FAIL] " if fails else "\nOK ") + "lane_auth_transient_robustness: %d failure(s)" % len(fails))
sys.exit(1 if fails else 0)
