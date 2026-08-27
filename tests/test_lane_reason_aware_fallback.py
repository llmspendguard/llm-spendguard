"""A flaky/quota lane must NOT be permanently bypassed — the failure REASON decides what is learned.

The bug: adapters._learn_from_fallback got only a boolean "did the API answer?", so a transient QUOTA miss was
indistinguishable from "prompt too big". Because _lane_ok_max starts at 0, the first quota miss at a ~33-char
prompt pinned _lane_big_prompt_ceiling=33 and routed every prompt >= 33 chars to the metered API for the whole
process — starving a working lane. Two compounding bugs fed it: the reset-window parser missed agy's COMPOUND
format ("Resets in 95h39m1s"), so the quota signal was lost; and a schema/first miss also pinned a size ceiling.

Pins:
  (a) the compound reset-window parses (agy's real format), and a bare/'N hours' form too;
  (b) a TRANSIENT (quota/rate) miss learns NO size ceiling — it is cooled until its (capped) reset window;
  (c) an ambiguous FIRST miss (no proven-good baseline) learns NO size ceiling — it backs off the model, retryable;
  (d) a size ceiling is learned ONLY when a failure is ABOVE a size the lane has PROVEN it can answer;
  (e) a real 'API also failed' cools the lane (down).
"""
import os
import sys
import tempfile

os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="sg-reason-")
os.environ.setdefault("SPENDGUARD_TEST_ISOLATED", "1")
os.environ.setdefault("SPENDGUARD_NO_AUTOINSTALL", "1")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from spendguard import adapters, antigravity_exec, resource_state                      # noqa: E402

fails = []


def check(name, cond):
    ok = bool(cond)
    print(("  [OK] " if ok else "  [FAIL] ") + name)
    if not ok:
        fails.append(name)


def _ceil(lane):
    return resource_state.size_ceiling(resource_state.lane_key(lane))


def _reset():
    resource_state._reset()                     # size_ceiling + cooldowns (whole-lane + per-model) all live here now


print("-- (a) the reset-window parses by TOKEN (preposition + compound duration), across phrasings --")
check("agy compound 'Resets in 95h39m1s' sums to seconds", antigravity_exec._reset_window_s("Individual quota reached. Resets in 95h39m1s") == 95 * 3600 + 39 * 60 + 1)
check("a bare 'resets in 900s' parses", antigravity_exec._reset_window_s("resets in 900s") == 900)
check("a worded 'resets in 2 hours' parses", antigravity_exec._reset_window_s("resets in 2 hours") == 7200)
check("'renews in …' parses (not just 'resets')", antigravity_exec._reset_window_s("quota renews in 95h39m1s") == 344341)
check("'available in 30m' parses", antigravity_exec._reset_window_s("rate limited, available in 30m") == 1800)
check("'try again in 45s' parses", antigravity_exec._reset_window_s("try again in 45s") == 45)
check("no duration token → None (a bare 'connection reset' is not a window)", antigravity_exec._reset_window_s("connection reset, no window") is None)

print("\n-- (b) a TRANSIENT (quota) miss learns NO size ceiling --")
_reset()
kind = adapters._learn_from_fallback("gemini", "a 40-character prompt that would trip 33", False, model="gemini-3.7-flash-medium", transient=True)
check("transient miss → 'transient', not 'unsuitable'", kind == "transient")
check("...and NO size ceiling was pinned (the whole-lane bypass can't happen)", _ceil("gemini") is None)

print("\n-- (c) an ambiguous FIRST miss (no proven-good) learns NO size ceiling --")
_reset()
kind = adapters._learn_from_fallback("gemini", "some prompt with no proven-good baseline yet", False, model="gemini-3.7-flash-medium")
check("first miss with proven-good=0 → 'model-cooled', not a size ceiling", kind == "model-cooled")
check("...NO size ceiling from an ambiguous first miss", _ceil("gemini") is None)
check("...the MODEL is backed off (retryable), not the whole lane", adapters._lane_model_cooling("gemini", "gemini-3.7-flash-medium"))

print("\n-- (d) a size ceiling IS learned above a PROVEN-good size --")
_reset()
adapters._lane_note_ok("gemini", "x" * 100)                      # proven it handles 100 chars
kind = adapters._learn_from_fallback("gemini", "y" * 5000, False, model="gemini-3.7-flash-medium")   # fails at 5000
check("failure ABOVE proven-good (100) → 'unsuitable' + a real ceiling", kind == "unsuitable" and _ceil("gemini") == 5000)
check("a genuinely big prompt is then routed to API", adapters._lane_too_big("gemini", "z" * 6000))
check("a prompt WITHIN proven-good is still served by the lane", not adapters._lane_too_big("gemini", "z" * 50))

print("\n-- (e) 'API also failed' cools the lane (down) --")
_reset()
kind = adapters._learn_from_fallback("gemini", "prompt", True, model="gemini-3.7-flash-medium")
check("api_failed=True → 'down' + lane cooled", kind == "down" and adapters._lane_cooling("gemini"))

print(f"\n{'[FAIL]' if fails else 'OK'} test_lane_reason_aware_fallback: {len(fails)} failure(s)")
sys.exit(1 if fails else 0)
