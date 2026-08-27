"""A quota-exhausted lane is DEMOTED (bug C). The gemini plan returned a quota envelope ('Resets in 162h') but was
still rated GOOD and kept receiving bulk work — because the refuse-billed path never cooled a lane, and the
API-outcome signal can't see quota (the metered twin answers fine). Now:
  • the EXECUTOR parses its OWN reset-window token into a STRUCTURED retry_after_s — never a keyword-guess of
    arbitrary error meaning (that would be an LLM's job); the presence of a fixed-shape 'resets in Nh' token IS
    the signal, and only its DURATION is parsed;
  • _call_once cools the lane for that window, INCLUDING on the --refuse-billed path;
  • the cooldown PERSISTS, so a fresh bulk process honors it, and _bulk_arms then excludes the lane.
Offline: the lane executor is stubbed; no network, no subprocess.
"""
import os
import sys
import tempfile
import time

os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="sg-quota-")
os.environ.setdefault("SPENDGUARD_TEST_ISOLATED", "1")
os.environ.setdefault("SPENDGUARD_NO_AUTOINSTALL", "1")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from spendguard import adapters, antigravity_exec, lane_balance, lane_bandit, lane_catalog, resource_state   # noqa: E402


def _gemini_cool_left():
    return resource_state.cool_until(resource_state.lane_key("gemini")) - __import__("time").time()


def ck(name, cond):
    ok = bool(cond)
    print(("  [OK] " if ok else "  [FAIL] ") + name)
    return [] if ok else [name]


fails = []

print("-- the executor PARSES its own reset-window TOKEN into a structured signal (no error-text classification) --")
fails += ck("'Resets in 162h' → 162h in seconds",
            antigravity_exec._reset_window_s("RESOURCE_EXHAUSTED: quota. Resets in 162h.") == 162 * 3600)
fails += ck("'retry after 30m' → 1800s", antigravity_exec._reset_window_s("rate limited, retry after 30m") == 1800)
fails += ck("'resets in 45s' → 45s", antigravity_exec._reset_window_s("resets in 45s") == 45)
fails += ck("an ordinary error has NO window (None → treated as a normal miss, never guessed as quota)",
            antigravity_exec._reset_window_s("lane down: connection reset") is None)
er = antigravity_exec._error_result("quota exhausted. Resets in 5h")
fails += ck("_error_result attaches retry_after_s when a window is present", er.get("retry_after_s") == 5 * 3600 and er.get("error"))
fails += ck("_error_result with no window is a plain error (no retry_after_s key)",
            "retry_after_s" not in antigravity_exec._error_result("boom"))

print("\n-- _lane_cool honors an explicit duration and PERSISTS across processes (unified resource_state store) --")
resource_state._reset()
adapters._lane_cool("gemini", seconds=162 * 3600)
fails += ck("the lane is cooling", adapters._lane_cooling("gemini"))
fails += ck("cooled for ~the requested window", abs(_gemini_cool_left() - 162 * 3600) < 5)
resource_state._reset()                                     # simulate a FRESH process: memory empty
fails += ck("memory cleared → not cooling in-memory", not adapters._lane_cooling("gemini"))
resource_state._load_state()                               # a new process reloads from disk at import
fails += ck("a fresh process RELOADS the persisted cooldown (honors 'until reset')", adapters._lane_cooling("gemini"))

print("\n-- a quota result cools the lane on the --refuse-billed path (which never cooled a lane before) --")
resource_state._reset(); resource_state._save()


class _QuotaLane:
    TIMEOUT_S = 300

    @staticmethod
    def run_prompt(prompt, system=None, model=None, timeout=None, reasoning=None):
        return antigravity_exec._error_result("RESOURCE_EXHAUSTED: quota. Resets in 162h")   # the real envelope shape


adapters._lane_for = lambda prov: ("gemini", _QuotaLane)     # force the gemini lane, then make it report quota
adapters._lane_too_big = lambda lane, prompt: False
adapters._lane_model_cooling = lambda lane, model: False
lane_balance.route_decision = lambda intent, model, reactive=False: (None, "no sub (test)")
r = adapters._call_once("gemini:g-low", "hi", max_tokens=100, no_metered_fallback=True)
fails += ck("refuse path still returns a refusal error row", "refused" in (r.get("error") or "").lower())
fails += ck("(C) the quota lane is now COOLING after the refuse-billed miss (it was not, before)",
            adapters._lane_cooling("gemini"))
_cap = adapters._max_quota_cool_s()
fails += ck("(C) cooled for the BOUNDED re-test window min(reset, cap), NOT the full 162h — an oscillating quota "
            "lane (agy) is re-tested periodically, never bypassed for days",
            0 < _gemini_cool_left() <= _cap + 5 and _gemini_cool_left() < 100 * 3600)

print("\n-- _bulk_arms EXCLUDES a cooling lane, so bulk stops handing it work --")
lane_catalog.arms = lambda flt=None: [("gemini", "g-high"), ("codex", "gpt-5.5")]
lane_catalog.lane_provider = lambda l: {"gemini": "gemini", "codex": "openai"}.get(l)
lane_bandit.arm_stats = lambda intent: {}                    # cold intent → every non-cooling lane kept
arms = lane_balance._bulk_arms("anyintent")                 # uses the REAL _arm_cooling → reads adapters._lane_cooling
fails += ck("(C) the cooling gemini lane is dropped from bulk arms (only codex remains)",
            {a[0] for a in arms} == {"codex"})

print(f"\n{'[FAIL]' if fails else 'OK'} test_lane_quota_cooldown: {len(fails)} failure(s)")
sys.exit(1 if fails else 0)
