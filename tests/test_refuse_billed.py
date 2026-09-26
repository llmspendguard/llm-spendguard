"""adapters no_metered_fallback (the engine behind `lanes --bulk --refuse-billed`), post the capability/failover work.
Guards _call_once directly, on STRUCTURAL signals (which branch produced the row), never a substring of the error prose:

  • a TASK miss — the lane RAN but returned no usable text, with NO error (_lane_reason='empty') — is refused under
    refuse_billed: the row is attributed to the LANE (executor is the lane), carries no cost and no text, and the
    metered API is never reached. Paying metered to retry a task the free lane found unsuitable is what refuse_billed
    opts out of.
  • a lane that is DOWN — its executor returned an ERROR (_lane_reason='lane_error': auth/token expired, CLI crash,
    rejected model) — is INFRASTRUCTURE failure, not an unsuitable task, so it STILL fails over through the ladder even
    under refuse_billed (Ash 2026-09-26: "a down lane still fails over"; budget_usd is the hard $0 cap) and the outage
    is SURFACED. The surface fires ONLY on the lane_error path, so its presence is the structural proof the down→ladder
    branch was taken rather than the $0 refusal.

Offline: the lane is stubbed to miss/err, the metered key and reactive substitute are stubbed out — no network, no spend.
"""
import os
import sys
import tempfile

os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="sg-refuse-")
os.environ.setdefault("SPENDGUARD_TEST_ISOLATED", "1")
os.environ.setdefault("SPENDGUARD_NO_AUTOINSTALL", "1")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from spendguard import adapters, config, lane_balance                                  # noqa: E402


def ck(name, cond):
    ok = bool(cond)
    print(("  [OK] " if ok else "  [FAIL] ") + name)
    return [] if ok else [name]


fails = []


class _EmptyLane:
    """A TASK miss: the lane RAN but returned no usable text, with NO error — the case refuse_billed suppresses ($0)."""
    TIMEOUT_S = 300

    @staticmethod
    def run_prompt(prompt, system=None, model=None, timeout=None, reasoning=None, max_tokens=None, **_kw):
        return {"text": "", "error": None}


class _DownLane:
    """A lane DOWN: the executor returned an ERROR — infra failure that must fail over even under refuse_billed."""
    TIMEOUT_S = 300

    @staticmethod
    def run_prompt(prompt, system=None, model=None, timeout=None, reasoning=None, max_tokens=None, **_kw):
        return {"error": "lane down (test)"}


adapters._lane_too_big = lambda lane, prompt: False
adapters._lane_model_cooling = lambda lane, model: False
config.api_key = lambda env: None                          # metered leg fails fast (no key) — offline, no spend
lane_balance.route_decision = lambda intent, model, reactive=False: (None, "no sub (test)")  # isolate: no reactive sub

print("-- a TASK miss (ran, empty, NO error) under refuse_billed → $0 refusal on the LANE, metered never reached --")
adapters._lane_for = lambda prov: ("gemini", _EmptyLane)
adapters._LANE_DOWN_SURFACED_AT.clear()
r = adapters._call_once("gemini:g-low", "hi", max_tokens=100, no_metered_fallback=True)
fails += ck("task miss + refuse_billed → row attributed to the LANE (not 'api')", r.get("executor") == "gemini")
fails += ck("...carries NO cost and NO text (a refusal, not an answer)", r.get("cost") is None and r.get("text") is None)
fails += ck("...and a task miss is NOT surfaced as a lane-down", "gemini" not in adapters._LANE_DOWN_SURFACED_AT)

print("\n-- a DOWN lane (executor error) under refuse_billed → fails over past the lane, surfaced --")
adapters._lane_for = lambda prov: ("gemini", _DownLane)
adapters._LANE_DOWN_SURFACED_AT.clear()
r = adapters._call_once("gemini:g-low", "hi", max_tokens=100, no_metered_fallback=True)
fails += ck("down lane + refuse_billed → SURFACED (structural proof the down→ladder branch ran, not the $0 refusal)",
            "gemini" in adapters._LANE_DOWN_SURFACED_AT)
fails += ck("...and the row is NOT a lane-attributed $0 refusal (it left the lane for the ladder)",
            not (r.get("executor") == "gemini" and r.get("cost") is None and r.get("text") is None))

print(f"\n{'[FAIL]' if fails else 'OK'} test_refuse_billed: {len(fails)} failure(s)")
sys.exit(1 if fails else 0)
