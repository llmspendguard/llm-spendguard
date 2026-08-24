"""adapters no_metered_fallback (the engine behind lanes --bulk --refuse-billed): a lane MISS returns a refusal
error, NEVER the metered API — $0 by construction. Guards _call_once directly: with no_metered_fallback=True a
failing lane yields 'refused: would bill metered API' and carries no cost; with it False the same miss is NOT
refused (it falls through toward the paid path — the unchanged default). Offline: the lane is stubbed to miss and
the reactive substitute is stubbed out, so the refuse-vs-fallback decision is isolated; no network.
"""
import os
import sys
import tempfile

os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="sg-refuse-")
os.environ.setdefault("SPENDGUARD_TEST_ISOLATED", "1")
os.environ.setdefault("SPENDGUARD_NO_AUTOINSTALL", "1")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from spendguard import adapters, lane_balance                                          # noqa: E402


def ck(name, cond):
    ok = bool(cond)
    print(("  [OK] " if ok else "  [FAIL] ") + name)
    return [] if ok else [name]


fails = []


class _MissLane:
    TIMEOUT_S = 300

    @staticmethod
    def run_prompt(prompt, system=None, model=None, timeout=None, reasoning=None):
        return {"error": "lane down (test)"}          # the lane MISSES every time


adapters._lane_for = lambda prov: ("gemini", _MissLane)     # force the lane path, then make it miss
adapters._lane_too_big = lambda lane, prompt: False
adapters._lane_model_cooling = lambda lane, model: False
lane_balance.route_decision = lambda intent, model, reactive=False: (None, "no sub (test)")  # isolate: no reactive sub

print("-- refuse: a lane miss returns a refusal, never the metered path ($0 by construction) --")
r = adapters._call_once("gemini:g-low", "hi", max_tokens=100, no_metered_fallback=True)
fails += ck("no_metered_fallback=True → 'refused' error", bool(r.get("error")) and "refused" in (r.get("error") or "").lower())
fails += ck("the refusal carries NO cost (billed axis stays $0)", not r.get("cost"))

print("\n-- default: the same miss is NOT refused (falls through toward metered — unchanged behavior) --")
r2 = adapters._call_once("gemini:g-low", "hi", max_tokens=100, no_metered_fallback=False)
fails += ck("no_metered_fallback=False → NOT a refusal (default fallback path)",
            "refused" not in (r2.get("error") or "").lower())

print(f"\n{'[FAIL]' if fails else 'OK'} test_refuse_billed: {len(fails)} failure(s)")
sys.exit(1 if fails else 0)
