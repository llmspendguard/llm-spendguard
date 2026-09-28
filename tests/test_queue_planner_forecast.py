"""C1 — the 429 FORECAST predicts a rate-limit breach from the LIVE governor state BEFORE it happens.

Offline + deterministic: it monkeypatches dispatch.queue_state to controlled snapshots that reproduce the REAL
conditions behind the corpus's 578 rate_limit failures (a burst whose backlog exceeds the vendor's tokens/minute
ceiling) and asserts the forecast's arithmetic + recommendation. $0 — no provider call, no ledger write.

The scenarios are the real shapes: a saturated vendor (backlog > 1 min of tpm → BATCH the load off realtime), an
approaching vendor (→ PACE), a vendor with headroom (→ OK), and a vendor with NO known tpm (→ UNKNOWN_TPM, the exact
place the first burst 429s — reported at_risk, never a confident ok).
"""
import os, sys, tempfile

if not os.environ.get("SPENDGUARD_TEST_ISOLATED"):
    os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"
    os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="spendguard-qplanner-")
    os.execv(sys.executable, [sys.executable] + sys.argv)

from spendguard import queue_planner, dispatch   # noqa: E402


class Checks:
    """A pass/fail collector — the tally lives on the instance (self), not module-global state, so each check records
    its result without the shared-mutation the coding doctrine flags."""

    def __init__(self):
        self.fails = 0

    def ck(self, label, cond, extra=""):
        if not cond:
            self.fails += 1
        print(f"  [{'OK' if cond else 'FAIL'}] {label}{('  — ' + extra) if extra and not cond else ''}")


def _with_state(state):
    """Run forecast() against a fabricated live governor snapshot, restoring the real reader after."""
    real = dispatch.queue_state
    dispatch.queue_state = lambda: {k: dict(v) for k, v in state.items()}
    try:
        return queue_planner.forecast(avg_call_tokens=3000)
    finally:
        dispatch.queue_state = real


def main():
    c = Checks()
    # A live governor snapshot reproducing the real burst: anthropic deep in backlog (28 calls at a 30k tpm ceiling →
    # 2.8 min of backlog → a 429 is imminent), openai approaching, zai with headroom, gemini with an UNKNOWN tpm.
    state = {
        "anthropic:claude-opus-4-8": {"limit": 8, "rpm": 0, "tpm": 30000, "in_flight": 8, "waiting": 20},
        "openai:gpt-5.5":            {"limit": 8, "rpm": 0, "tpm": 100000, "in_flight": 5, "waiting": 15},
        "zai:glm-5.3":               {"limit": 8, "rpm": 0, "tpm": 200000, "in_flight": 2, "waiting": 3},
        "gemini:gemini-3-flash":     {"limit": 8, "rpm": 0, "tpm": 0, "in_flight": 4, "waiting": 10},
    }
    fc = _with_state(state)
    v = fc["vendors"]
    print("== the forecast predicts the breach from live state (backlog_tokens / tpm = minutes of backlog) ==")
    # anthropic: (8+20)*3000 = 84000 / 30000 = 2.8 min → BATCH (offload the burst; do NOT keep hammering realtime)
    c.ck("saturated vendor (2.8 min backlog) → BATCH", v["anthropic:claude-opus-4-8"]["recommend"] == "batch",
         str(v["anthropic:claude-opus-4-8"]))
    c.ck("...and its risk is the real arithmetic (2.8)", abs(v["anthropic:claude-opus-4-8"]["risk"] - 2.8) < 0.01)
    # openai: (5+15)*3000 = 60000 / 100000 = 0.6 min → PACE (approaching, between 0.5 and 1.0)
    c.ck("approaching vendor (0.6 min) → PACE", v["openai:gpt-5.5"]["recommend"] == "pace", str(v["openai:gpt-5.5"]))
    # zai: (2+3)*3000 = 15000 / 200000 = 0.075 min → OK
    c.ck("vendor with headroom (0.075 min) → OK", v["zai:glm-5.3"]["recommend"] == "ok", str(v["zai:glm-5.3"]))
    # gemini: tpm unknown → cannot forecast → UNKNOWN_TPM, flagged at_risk (the first-burst blind spot)
    c.ck("unknown-tpm vendor → UNKNOWN_TPM (not a confident ok)",
         v["gemini:gemini-3-flash"]["recommend"] == "unknown_tpm", str(v["gemini:gemini-3-flash"]))
    c.ck("unknown-tpm risk is None (unmeasurable, never invented)", v["gemini:gemini-3-flash"]["risk"] is None)

    print("\n== at_risk + counts summarise the actionable vendors ==")
    c.ck("at_risk names the three non-ok vendors (batch+pace+unknown)", set(fc["at_risk"]) ==
         {"anthropic:claude-opus-4-8", "openai:gpt-5.5", "gemini:gemini-3-flash"}, str(fc["at_risk"]))
    c.ck("recommend_counts tally", fc["recommend_counts"].get("batch") == 1 and fc["recommend_counts"].get("pace") == 1
         and fc["recommend_counts"].get("ok") == 1 and fc["recommend_counts"].get("unknown_tpm") == 1,
         str(fc["recommend_counts"]))

    print("\n== an idle governor forecasts nothing (no false alarm) ==")
    idle = _with_state({})
    c.ck("idle → no vendors, nothing at risk", idle["vendors"] == {} and idle["at_risk"] == [])

    print("\n== forecast_summary() renders without raising ==")
    real = dispatch.queue_state
    dispatch.queue_state = lambda: {k: dict(v) for k, v in state.items()}
    try:
        s = queue_planner.forecast_summary()
    finally:
        dispatch.queue_state = real
    c.ck("forecast_summary is a non-empty string naming BATCH", isinstance(s, str) and "BATCH" in s, s[:80])

    print(f"\n{'[FAIL]' if c.fails else 'OK'} test_queue_planner_forecast: {c.fails} failure(s)")
    return 1 if c.fails else 0


if __name__ == "__main__":
    sys.exit(main())
