"""Error-aware lane fallback: the SYSTEM routes lane-vs-API from the API-fallback OUTCOME, not the lane's error
text — so one big prompt a subscription lane can't handle does NOT cool the lane for every later (small) prompt.

MEASURED (2026-08-14, a 4-LLM code review): a big file made the claude-code lane return error_max_turns; the old
code cooled the WHOLE lane for 900s, and every prompt after it — even tiny ones — went to the paid API. The fix
uses a fact the system already has: it falls back to the API anyway, so the API's outcome on the SAME prompt
settles it — API answered where the lane didn't ⇒ the lane was UNSUITABLE for this prompt (keep it, learn the
size), API failed too ⇒ genuinely down (cool it). One signal, every lane, no error-string interpretation.

Offline, isolated home. The 'API answered' branch is unit-tested directly (a live API success can't run offline);
the 'API failed' branch runs end-to-end through the real recursion (lane errors + no key → API fails → cooled).
"""
import os
import sys
import tempfile
import types

if not os.environ.get("SPENDGUARD_TEST_ISOLATED"):
    os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"
    os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="spendguard-lanefb-")
    os.execv(sys.executable, [sys.executable] + sys.argv)

from spendguard import adapters, resource_state   # noqa: E402

fails = 0


def ck(label, cond, extra=""):
    global fails
    if not cond:
        fails += 1
    print(f"  [{'OK' if cond else 'FAIL'}] {label}{('  — ' + extra) if extra and not cond else ''}")


# ── 1. the decision is REASON-AWARE: a SIZE ceiling needs a PROVEN-GOOD baseline, never an ambiguous first miss ─
# The API OUTCOME still decides lane-vs-API, but a first miss with NO proven-good size is AMBIGUOUS
# (schema/content/a not-yet-parsed transient), so it backs off the MODEL (retryable) rather than pin a permanent
# whole-lane size ceiling — the poison that starved a working lane. A size ceiling is learned only once the lane
# has PROVEN a size and then failed ABOVE it (section 1b).
adapters._lane_big_prompt_ceiling.clear()
adapters._lane_ok_max.clear()
resource_state._reset()
k = adapters._learn_from_fallback("laneA", "x" * 5000, api_failed=False, model="m")
ck("a first miss with NO proven-good → 'model-cooled' (retryable), NOT a permanent size ceiling",
   k == "model-cooled" and "laneA" not in adapters._lane_big_prompt_ceiling)
ck("...the whole lane is KEPT (not cooled); only THAT model backs off",
   not adapters._lane_cooling("laneA") and adapters._lane_model_cooling("laneA", "m"))
ck("a TRANSIENT (quota/rate) miss learns nothing about suitability — no size ceiling",
   adapters._learn_from_fallback("laneA", "x" * 5000, api_failed=False, transient=True) == "transient"
   and "laneA" not in adapters._lane_big_prompt_ceiling)

# ── 1b. a proven-good size makes a SMALLER failure content-specific, not a size ceiling (robustness) ──────────
adapters._lane_ok_max.clear()
adapters._lane_big_prompt_ceiling.pop("laneD", None)
adapters._lane_note_ok("laneD", "x" * 5000)                          # the lane ANSWERED a 5000-char prompt
adapters._learn_from_fallback("laneD", "x" * 3000, api_failed=False)  # a SMALLER prompt then fails unsuitably
ck("a failure BELOW a proven-good size does NOT set a routing ceiling (one anomaly can't disable a working lane)",
   "laneD" not in adapters._lane_big_prompt_ceiling)
adapters._learn_from_fallback("laneD", "x" * 8000, api_failed=False)  # a LARGER prompt fails → that IS a size signal
ck("a failure ABOVE the proven-good size DOES set the ceiling", adapters._lane_big_prompt_ceiling.get("laneD") == 8000)

# ── 2. API failed too → 'down' → cool the lane ───────────────────────────────────────────────────────────────
k = adapters._learn_from_fallback("laneB", "p", api_failed=True)
ck("API fallback also failed → 'down' → the lane is cooled", k == "down" and adapters._lane_cooling("laneB"))

# ── 3. end-to-end DOWN path through the real recursion: lane errors + no API key → API fails → lane cooled ────
fake = types.SimpleNamespace(TIMEOUT_S=60,
                             run_prompt=lambda prompt, system=None, model=None, timeout=None, **_kw: {"error": "boom", "text": None})
_real_lane_for, _real_key = adapters._lane_for, adapters.config.api_key
adapters._lane_for = lambda prov: ("laneC", fake) if prov == "anthropic" else None
adapters.config.api_key = lambda name: None            # the API fallback fails fast with no key
adapters._lane_big_prompt_ceiling.pop("laneC", None)
resource_state.clear_cooldown(resource_state.lane_key("laneC"))
try:
    r = adapters._call_once("anthropic:claude-opus-4-8", "review this", max_tokens=1000, timeout_s=5)
finally:
    adapters._lane_for, adapters.config.api_key = _real_lane_for, _real_key
ck("end-to-end: lane errors + API (no key) fails → the result carries the API error (no false success)", bool(r.get("error")))
ck("...and because the API fallback failed too, the lane was cooled (down)", adapters._lane_cooling("laneC"))
ck("...and it was NOT mislearned as an unsuitable-size ceiling", "laneC" not in adapters._lane_big_prompt_ceiling)

print(f"\n{'[FAIL]' if fails else 'OK'} test_lane_fallback_is_error_aware: {fails} failure(s)")
sys.exit(1 if fails else 0)
