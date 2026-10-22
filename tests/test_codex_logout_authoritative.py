"""The Codex logout banner follows a fresh authoritative auth verdict: True is silent; False still alerts."""
import os
import pathlib
import subprocess
import sys
import tempfile

_ISOLATION_KEY = "SPENDGUARD_TEST_ISOLATED"
_HOME_KEY = "SPENDGUARD_HOME"
if os.environ.get(_ISOLATION_KEY) != "1":
    child_environment = os.environ.copy()
    child_environment[_ISOLATION_KEY] = "1"
    child_environment[_HOME_KEY] = tempfile.mkdtemp(prefix="spendguard-codex-auth-authoritative-")
    raise SystemExit(subprocess.run([sys.executable, __file__], env=child_environment).returncode)

os.environ.setdefault("SPENDGUARD_NO_AUTOINSTALL", "1")
_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))

from spendguard import adapters, lanes, reliability  # noqa: E402

failures = []


def check(label, condition):
    print(f"  [{'OK' if condition else 'FAIL'}] {label}")
    if not condition:
        failures.append(label)


original_lane_status = lanes.lane_auth_status
original_surface = adapters._surface_lane_auth
original_note_down = reliability.note_lane_auth_down
try:
    status_calls = []

    def logged_in_status(lane, ttl=None):
        status_calls.append((lane, ttl))
        return {"authed": True, "cmd": "login-command"}

    lanes.lane_auth_status = logged_in_status
    logged_in = adapters._authoritative_lane_auth_status("codex")
    check("authoritative Codex auth bypasses cached state", status_calls == [("codex", 0)])
    emitted = []
    recorded = []
    adapters._surface_lane_auth = lambda *args: emitted.append(args)
    reliability.note_lane_auth_down = lambda *args: recorded.append(args)
    check("logged-in auth does not fire the logout banner",
          adapters._surface_confirmed_lane_logout("codex", logged_in) is False and not emitted and not recorded)

    logged_out = {"authed": False, "cmd": "login-command"}
    check("logged-out auth still fires and records the logout banner",
          adapters._surface_confirmed_lane_logout("codex", logged_out) is True and
          emitted == [("codex", "login-command")] and recorded == [("codex", "login-command")])
finally:
    lanes.lane_auth_status = original_lane_status
    adapters._surface_lane_auth = original_surface
    reliability.note_lane_auth_down = original_note_down

print(f"\n{'OK' if not failures else 'FAIL'} test_codex_logout_authoritative: {len(failures)} failure(s)")
raise SystemExit(1 if failures else 0)
