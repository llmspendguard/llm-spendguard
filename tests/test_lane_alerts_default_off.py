"""A lane is spendguard's INTERNAL $0-routing detail. A CONSUMER of the library must never see lane commentary on the
CALL PATH — the call succeeds regardless of which route served (a down lane fails over to the metered API), and cost is
reported separately. So every call-path lane alert (_lane_chatter, and the down/logged-out surfacers) is OFF by default
and only an OPERATOR opts in with advisor.lane_call_alerts=on. The operator's DURABLE lane-health view (spendguard
doctor / reliability.note_lane_*) is a different surface and is NOT gated by this flag.

This guards the "the user should not care about lane or not lane" contract. Offline, $0 — no LLM, no network.
"""
import contextlib
import io
import os
import sys
import tempfile

os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="sg-laneabuse-")
os.environ.setdefault("SPENDGUARD_TEST_ISOLATED", "1")
os.environ.setdefault("SPENDGUARD_NO_AUTOINSTALL", "1")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from spendguard import adapters, config   # noqa: E402

fails = []


def ck(name, cond):
    print(("  [OK] " if cond else "  [FAIL] ") + name)
    if not cond:
        fails.append(name)


def _stderr_of(fn):
    buf = io.StringIO()
    with contextlib.redirect_stderr(buf):
        fn()
    return buf.getvalue()


# the REAL default: the flag is unset, so config._cfg_get returns the "off" default → the consumer sees nothing.
print("-- DEFAULT (flag unset): the call-path lane alerts are SILENT to the consumer --")
ck("_lane_chatter is silent by default", _stderr_of(lambda: adapters._lane_chatter("[spendguard] codex lane failed → metered")) == "")
ck("_surface_lane_down is silent by default", _stderr_of(lambda: adapters._surface_lane_down("codex", "boom")) == "")
ck("_surface_lane_auth is silent by default", _stderr_of(lambda: adapters._surface_lane_auth("codex", "codex login")) == "")

# operator opt-in flips it on — same functions now print (for an operator watching a live batch).
print("-- OPERATOR opt-in (advisor.lane_call_alerts=on): the same alerts print --")
_orig = config._cfg_get
_flag = ["on"]
def _cfg(section, key, default=None):
    if (section, key) == ("advisor", "lane_call_alerts"):
        return _flag[0]
    return _orig(section, key, default)
config._cfg_get = _cfg
try:
    ck("with the flag on, _lane_chatter prints the operator note",
       "codex lane failed" in _stderr_of(lambda: adapters._lane_chatter("[spendguard] codex lane failed → metered")))
    ck("with the flag on, _surface_lane_auth states the logout + fix",
       "LOGGED OUT" in _stderr_of(lambda: adapters._surface_lane_auth("codex", "codex login")))
    # and OFF again means silent, even explicitly set
    _flag[0] = "off"
    ck("explicit off → silent again", _stderr_of(lambda: adapters._lane_chatter("[spendguard] anything")) == "")
finally:
    config._cfg_get = _orig

print("-- the OPERATOR's durable lane-health surface is a DIFFERENT layer, not gated by this consumer flag --")
import inspect   # noqa: E402
_src = inspect.getsource(adapters._surface_lane_auth) + inspect.getsource(adapters._surface_lane_down)
ck("the surfacers still point the OPERATOR at note_lane_* / doctor (recorded regardless of the consumer flag)",
   "note_lane_auth_down" in _src and ("doctor" in _src or "note_lane_down" in _src))

print(f"\n{'[FAIL]' if fails else 'OK'} test_lane_alerts_default_off: {len(fails)} failure(s)")
sys.exit(1 if fails else 0)
