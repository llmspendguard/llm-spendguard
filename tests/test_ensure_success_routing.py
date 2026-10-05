"""Items #5/#6 acceptance: measured-headroom bandit tilt and ensure-success lane failover.

Offline: every lane/API is a fake; the metered twin is intercepted before any SDK/network call. The fake metered
success records through calls.record_call so fell_from is verified on the durable ledger, not merely on a dict.
"""
import contextlib
import io
import os
import sqlite3
import sys
import tempfile

os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="sg-ensure-success-")
os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"
os.environ.setdefault("SPENDGUARD_NO_AUTOINSTALL", "1")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from spendguard import adapters, calls, config, lane_balance, lane_bandit, lanes  # noqa: E402


def check(results, label, condition):
    results.append(bool(condition))
    print(f"  [{'OK' if condition else 'FAIL'}] {label}")


def main():
    results = []

    print("-- measured headroom tilts equal-quality bandit arms; unknown leaves the old tie unchanged --")
    original_stats = lane_bandit.arm_stats
    original_idle = lane_bandit._idle_bonus
    original_headroom = lanes.lane_headroom
    try:
        lane_bandit.arm_stats = lambda intent: {
            ("codex", "gpt-x"): {"trials": 2, "winrate": 0.8},
            ("gemini", "gem-x"): {"trials": 2, "winrate": 0.8}}
        lane_bandit._idle_bonus = lambda lane: 1.0
        lanes.lane_headroom = lambda do_fetch=False: [
            {"lane": "codex", "provider": "openai", "known": True, "remaining_pct": 80, "reset_ts": None},
            {"lane": "gemini", "provider": "gemini", "known": True, "remaining_pct": 20, "reset_ts": None}]
        original_cfg = lane_bandit._bcfg
        lane_bandit._bcfg = lambda name, default: 0.0 if name == "bandit_epsilon" else default
        arms = [("gemini", "gem-x"), ("codex", "gpt-x")]
        check(results, "equal measured quality prefers the lane with more measured headroom",
              lane_bandit.choose_arm("intent", arms) == ("codex", "gpt-x"))
        lanes.lane_headroom = lambda do_fetch=False: []
        check(results, "unknown headroom is neutral (the prior equal-quality tie is unchanged)",
              lane_bandit.choose_arm("intent", arms) == arms[0])
        lane_bandit._bcfg = original_cfg
    finally:
        lane_bandit.arm_stats = original_stats
        lane_bandit._idle_bonus = original_idle
        lanes.lane_headroom = original_headroom

    class DownLane:
        TIMEOUT_S = 30

        @staticmethod
        def run_prompt(*args, **kwargs):
            return {"error": "fake lane unavailable", "text": None}

    original_lane_for = adapters._lane_for
    original_too_big = adapters._lane_too_big
    original_model_cooling = adapters._lane_model_cooling
    original_quality_subs = lane_balance.quality_equivalent_free_substitutes
    original_call = adapters.call
    original_once = adapters._call_once
    adapters._lane_for = lambda provider: ("claude-code", DownLane) if provider == "anthropic" else None
    adapters._lane_too_big = lambda lane, prompt: False
    adapters._lane_model_cooling = lambda lane, model: False
    try:
        print("\n-- fungible miss exhausts a measured-good $0 substitute before metered --")
        lane_balance.quality_equivalent_free_substitutes = lambda intent, model, excluded=None: ["gemini:gem-x"]
        seen = []

        def free_success(model, prompt, **kwargs):
            seen.append((model, kwargs.get("no_metered_fallback")))
            return {"text": "free answer", "error": None, "cost": 0.0, "executor": "gemini",
                    "provider": "gemini", "model": "gem-x"}

        adapters.call = free_success
        row = original_once("anthropic:claude-x", "task", max_tokens=100)
        check(results, "task succeeds on the second $0 lane", row.get("text") == "free answer")
        check(results, "substitute attempt is explicitly free-only", seen == [("gemini:gem-x", True)])

        print("\n-- all $0 lanes miss: metered succeeds, is loud, and ledger records fell_from --")
        adapters.call = lambda model, prompt, **kwargs: {
            "text": None, "error": "fake free miss", "cost": None, "executor": "gemini"}
        metered_models = []

        def intercepted_once(model, prompt, **kwargs):
            if kwargs.get("_skip_lane"):
                metered_models.append(model)
                calls.record_call("anthropic", model.split(":", 1)[-1], "realtime", 0.125,
                                  in_tok=10, out_tok=5, intent="ensure-success")
                return {"text": "metered answer", "error": None, "cost": 0.125, "executor": "api",
                        "provider": "anthropic", "model": model.split(":", 1)[-1]}
            return original_once(model, prompt, **kwargs)

        adapters._call_once = intercepted_once
        stderr = io.StringIO()
        with calls.context(intent="ensure-success"), contextlib.redirect_stderr(stderr):
            row2 = intercepted_once("anthropic:claude-x", "task", max_tokens=100)
        check(results, "metered last resort succeeds", row2.get("text") == "metered answer")
        check(results, "fallback stayed on the original model's metered twin",
              metered_models == ["anthropic:claude-x"])
        check(results, "fallback is loud and names unavailable lanes",
              "fell over to metered $0.1250" in stderr.getvalue() and "claude-code" in stderr.getvalue())
        check(results, "returned output carries fell_from + structured loud notice",
              row2.get("fell_from") == "claude-code" and "fallback_notice" in row2)
        con = sqlite3.connect(config.db_path())
        ledger_fell_from = con.execute(
            "SELECT fell_from FROM calls WHERE intent='ensure-success' ORDER BY rowid DESC LIMIT 1").fetchone()
        con.close()
        check(results, "metered ledger row records fell_from", ledger_fell_from == ("claude-code",))

        print("\n-- pinned miss never swaps; explicit refuse-billed makes zero metered attempts --")
        substitute_queries = []
        lane_balance.quality_equivalent_free_substitutes = lambda *args, **kwargs: substitute_queries.append(args) or ["gemini:gem-x"]
        metered_models.clear()
        with contextlib.redirect_stderr(io.StringIO()):
            pinned = intercepted_once("anthropic:claude-x", "task", max_tokens=100, _no_sub=True)
        check(results, "pinned call never asks for a substitute", not substitute_queries)
        check(results, "pinned call meters the same model", pinned.get("text") == "metered answer"
              and metered_models == ["anthropic:claude-x"])
        metered_models.clear()
        refused = intercepted_once("anthropic:claude-x", "task", max_tokens=100,
                                   no_metered_fallback=True)
        check(results, "refuse-billed returns a free error row", refused.get("text") is None
              and refused.get("cost") is None and str(refused.get("error") or "").startswith("refused"))
        check(results, "refuse-billed made zero metered attempts", metered_models == [])
    finally:
        adapters._lane_for = original_lane_for
        adapters._lane_too_big = original_too_big
        adapters._lane_model_cooling = original_model_cooling
        lane_balance.quality_equivalent_free_substitutes = original_quality_subs
        adapters.call = original_call
        adapters._call_once = original_once

    failures = results.count(False)
    print(f"\n{'[FAIL]' if failures else 'OK'} test_ensure_success_routing: {failures} failure(s)")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
