"""ON-LANE TRANSIENT RETRY — a $0 lane that has PROVEN it can answer a prompt this size but fails THIS time (a
momentary plan throttle / at-capacity) is retried ONCE on the free lane before spilling to PAID metered. Keeps work
free + resilient. Uses the proven-good watermark FACT, never the error string (the doctrine), and a DELIBERATE stop
(deadline/refusal) still HALTS — it is never swallowed into an error dict and retried.

Pins: (1) a proven-good lane's transient blip retries and recovers on the lane; (2) a lane with NO proven-good
history also confirms the miss before paid recovery; (3) a deliberate stop propagates, never retried.

Hermetic: adapters._lane_for stubbed to fake lanes; no network."""
import os
import sys
import tempfile

os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="spendguard-laneretry-")
os.environ.setdefault("SPENDGUARD_TEST_ISOLATED", "1")
os.environ.setdefault("SPENDGUARD_NO_AUTOINSTALL", "1")
# This test isolates transient retry behavior on its fake Claude lanes. The dedicated quota-preflight acceptance
# script covers the default-on Anthropic avoidance policy and its authorization behavior.
os.environ["SPENDGUARD_AVOID_CLAUDE_LANE"] = "off"
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from spendguard import adapters, resource_state, dispatch, lane_balance, lane_queue   # noqa: E402

fails = []


def ck(name, cond):
    print(("  [OK] " if cond else "  [FAIL] ") + name)
    if not cond:
        fails.append(name)


_n = {"flaky": 0, "fail": 0}


class _FlakyLane:                          # a transient blip: fails the FIRST call, answers the SECOND
    TIMEOUT_S, MIN_TIMEOUT_S = 300, 1

    @staticmethod
    def run_prompt(prompt, system=None, model=None, timeout=None, reasoning=None, max_tokens=None):
        _n["flaky"] += 1
        if _n["flaky"] == 1:
            return {"error": "momentary plan throttle"}
        return {"text": "recovered", "in_tok": 3, "out_tok": 2, "latency": 0.1, "error": None}


class _AlwaysFail:                         # never answers
    TIMEOUT_S, MIN_TIMEOUT_S = 300, 1

    @staticmethod
    def run_prompt(prompt, system=None, model=None, timeout=None, reasoning=None, max_tokens=None):
        _n["fail"] += 1
        return {"error": "still failing"}


class _DeadlineLane:                       # raises a DELIBERATE stop (a governor deadline)
    TIMEOUT_S, MIN_TIMEOUT_S = 300, 1

    @staticmethod
    def run_prompt(prompt, system=None, model=None, timeout=None, reasoning=None, max_tokens=None):
        raise dispatch.DispatchTimeout("deliberate stop from the lane")


print("-- a PROVEN-GOOD lane's transient blip is retried once and recovers on the $0 lane --")
adapters._lane_for = lambda prov: ("claude-code", _FlakyLane) if prov == "anthropic" else None
resource_state.note_proven_good(resource_state.lane_key("claude-code"), 1000)   # it answered a big prompt before
_n["flaky"] = 0
r = adapters._call_once("anthropic:claude-x", "hi", max_tokens=100)
ck("retried once (2 lane calls) and recovered on the lane — no metered spill",
   _n["flaky"] == 2 and r.get("text") == "recovered" and r.get("executor") == "claude-code")

print("\n-- a lane with NO proven-good history still confirms a miss before metered --")
adapters._lane_for = lambda prov: ("codexfake", _AlwaysFail) if prov == "openai" else None
_n["fail"] = 0
_original_quality_substitutes = lane_balance.quality_equivalent_free_substitutes
_original_hold_free_recovery = lane_queue.hold_free_recovery
try:
    # This test owns the confirmation-retry rung. The complete substitute→queue→metered ladder is exercised by
    # test_ensure_success_routing.py; do not let its additional primary probes inflate this rung's attempt count.
    lane_balance.quality_equivalent_free_substitutes = lambda *args, **kwargs: []
    lane_queue.hold_free_recovery = lambda *args, **kwargs: {
        "error": "isolated recovery queue exhausted", "_free_hold": {"deadline_exhausted": True}}
    adapters._call_once("openai:gpt-5.5", "hi", max_tokens=100, no_metered_fallback=True)
finally:
    lane_balance.quality_equivalent_free_substitutes = _original_quality_substitutes
    lane_queue.hold_free_recovery = _original_hold_free_recovery
ck("no proven-good history → bounded confirmation attempts", _n["fail"] == 2)

print("\n-- a DELIBERATE stop from the lane PROPAGATES (halts), never swallowed + retried --")
adapters._lane_for = lambda prov: ("claude-code", _DeadlineLane) if prov == "anthropic" else None
resource_state.note_proven_good(resource_state.lane_key("claude-code"), 1000)
raised = False
try:
    adapters._call_once("anthropic:claude-y", "hi", max_tokens=100)
except dispatch.DispatchTimeout:
    raised = True
ck("a deliberate stop halts (propagates), not downgraded to an error dict + retried", raised)

print(f"\n{'[FAIL]' if fails else 'OK'} test_lane_transient_retry: {len(fails)} failure(s)")
sys.exit(1 if fails else 0)
