"""A transient `codex login status` non-zero must NOT be reported as a logout, and a positive auth must retract a
stale 'logged out' banner — so codex stays robustly usable ($0 lane) instead of flapping to a re-login nag + metered
fallback while the token is in fact valid. Offline: subprocess + exec are monkeypatched; no codex CLI, no spend."""
import os
import sys
import tempfile

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
    """A subprocess.run stand-in that yields the given returncodes in order across successive calls."""
    seq = list(return_codes)
    calls = {"n": 0}

    def _run(*a, **k):
        rc = seq[calls["n"]] if calls["n"] < len(seq) else seq[-1]
        calls["n"] += 1
        return _Proc(rc)

    return _run, calls


# --- (A) auth_status confirms a logout before reporting False -------------------------------------------------
_orig_run, _orig_bin, _orig_sleep = codex_exec.subprocess.run, codex_exec._bin, codex_exec.time.sleep
codex_exec._bin = lambda: "/usr/bin/true"              # a resolvable exe so auth_status runs the (patched) subprocess
codex_exec.time.sleep = lambda *_: None                # don't actually wait the re-check backoff in a test
try:
    run_ok, _ = _scripted_run([0])
    codex_exec.subprocess.run = run_ok
    fails += ck("a clean status (rc 0) is authed with no re-check", codex_exec.auth_status() == {"authed": True})

    run_transient, calls_t = _scripted_run([1, 0])     # one non-zero, then recovers
    codex_exec.subprocess.run = run_transient
    r_transient = codex_exec.auth_status()
    fails += ck("a TRANSIENT non-zero (1 then 0) is NOT reported as logged out", r_transient == {"authed": True})
    fails += ck("...and it took exactly one confirming re-check (2 status calls)", calls_t["n"] == 2)

    run_down, _ = _scripted_run([1, 1])                # non-zero on both — a real logout
    codex_exec.subprocess.run = run_down
    fails += ck("a CONFIRMED logout (1 then 1) is reported authed=False", codex_exec.auth_status() == {"authed": False})
finally:
    codex_exec.subprocess.run, codex_exec._bin, codex_exec.time.sleep = _orig_run, _orig_bin, _orig_sleep

# --- (B) a positive auth retracts a stale 'logged out' banner (self-heal, no served call needed) ---------------
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
