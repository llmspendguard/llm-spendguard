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

from spendguard import adapters   # noqa: E402

fails = 0


def ck(label, cond, extra=""):
    global fails
    if not cond:
        fails += 1
    print(f"  [{'OK' if cond else 'FAIL'}] {label}{('  — ' + extra) if extra and not cond else ''}")


# ── 1. the decision, taken from the API OUTCOME (pure) ───────────────────────────────────────────────────────
adapters._lane_big_prompt_ceiling.clear()
k = adapters._learn_from_fallback("laneA", "x" * 5000, api_failed=False)
ck("API answered where the lane didn't → 'unsuitable' (lane KEPT, not cooled)",
   k == "unsuitable" and not adapters._lane_cooling("laneA"))
ck("...and the failing size is recorded as the routing ceiling", adapters._lane_big_prompt_ceiling.get("laneA") == 5000)
ck("a prompt >= the ceiling now routes straight to API",
   adapters._lane_too_big("laneA", "y" * 5000) and adapters._lane_too_big("laneA", "y" * 9000))
ck("...but a small prompt still uses the lane", not adapters._lane_too_big("laneA", "y" * 100))
adapters._learn_from_fallback("laneA", "x" * 3000, api_failed=False)
ck("the ceiling is the SMALLEST failing size (a smaller failure lowers it)",
   adapters._lane_big_prompt_ceiling.get("laneA") == 3000)

# ── 2. API failed too → 'down' → cool the lane ───────────────────────────────────────────────────────────────
k = adapters._learn_from_fallback("laneB", "p", api_failed=True)
ck("API fallback also failed → 'down' → the lane is cooled", k == "down" and adapters._lane_cooling("laneB"))

# ── 3. end-to-end DOWN path through the real recursion: lane errors + no API key → API fails → lane cooled ────
fake = types.SimpleNamespace(TIMEOUT_S=60,
                             run_prompt=lambda prompt, system=None, model=None, timeout=None: {"error": "boom", "text": None})
_real_lane_for, _real_key = adapters._lane_for, adapters.config.api_key
adapters._lane_for = lambda prov: ("laneC", fake) if prov == "anthropic" else None
adapters.config.api_key = lambda name: None            # the API fallback fails fast with no key
adapters._lane_big_prompt_ceiling.pop("laneC", None)
adapters._lane_cooldown.pop("laneC", None)
try:
    r = adapters._call_once("anthropic:claude-opus-4-8", "review this", max_tokens=1000, timeout_s=5)
finally:
    adapters._lane_for, adapters.config.api_key = _real_lane_for, _real_key
ck("end-to-end: lane errors + API (no key) fails → the result carries the API error (no false success)", bool(r.get("error")))
ck("...and because the API fallback failed too, the lane was cooled (down)", adapters._lane_cooling("laneC"))
ck("...and it was NOT mislearned as an unsuitable-size ceiling", "laneC" not in adapters._lane_big_prompt_ceiling)

print(f"\n{'[FAIL]' if fails else 'OK'} test_lane_fallback_is_error_aware: {fails} failure(s)")
sys.exit(1 if fails else 0)
