"""The bandit accounts for realized cost (incl. reasoning) AGENTICALLY — the bake-off VALUE judge weighs quality vs cost
with the 70/30 as PROMPT guidance — while routing (arm_score / choose_arm) stays PURE STATE with NO cost formula.
Caller-feedback IMPROVEMENT #2, part A2.1 (redesigned per Ash: "fine if it's agentic but the prompt can leverage 70/30").

The gap A2.1 closed: the router treated a reasoning model that emits 7x the norm as if it were free. The earlier fix put
a 70/30 weighted SUM in arm_score — but a fixed formula deciding "is this quality lift worth the cost?" is exactly the
arithmetic-for-a-judgement that the agentic_decisions doctrine (rightly) refuses. So the tradeoff moved to the ONE place
that already makes an LLM call and already runs only occasionally — the bake-off judge:

  • arm_score == winrate × idle-fill — a PURE value-reward, NO cost term, NO realized_costs argument (co_argcount 2);
  • _value_judge_prompt, when both arms' realized $/call are known, LEVERAGES the 70/30 as explicit guidance + both
    cost figures + frames the call as a JUDGEMENT ("not arithmetic", "worth its extra cost") — the LLM decides;
  • cold (no costs) → a pure-quality prompt (no percentages, no cost line);
  • the 70/30 is CONFIG (dialling bandit_quality_weight changes the prompt's stated split);
  • _intent_realized_costs prices a higher-output (reasoning) arm above a lean one (the reasoning tax is SEEN);
  • run_bakeoff computes those realized costs and HANDS them to the judge (the wiring).

Offline, isolated SPENDGUARD_HOME, zero spend (no LLM is called — the judge is monkeypatched where exercised)."""
import os
import sys
import tempfile

os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="sg-bandit-vj-")
os.environ.setdefault("SPENDGUARD_TEST_ISOLATED", "1")
os.environ.setdefault("SPENDGUARD_NO_AUTOINSTALL", "1")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from spendguard import lane_bandit as lb, config  # noqa: E402


class Checks:
    def __init__(self):
        self.fails = []

    def __call__(self, label, cond, extra=""):
        if not cond:
            self.fails.append(label)
        print(f"  [{'OK' if cond else 'FAIL'}] {label}{('  — ' + extra) if extra and not cond else ''}")


ck = Checks()
lb._idle_bonus = lambda lane: 1.0                     # neutralize the capacity tilt — isolate the value-reward

# ── A. arm_score is a PURE value-reward (winrate × idle), with NO cost term / NO realized-cost argument ───────────────
INTENT = "vj-intent"
HI, LO = ("codex", "hi-value"), ("codex", "lo-value")
for _ in range(9):
    lb.record_trial(INTENT, HI[0], HI[1], won=1.0)   # HI wins the (cost-aware) value bake-off almost always
    lb.record_trial(INTENT, LO[0], LO[1], won=0.0)
lb.record_trial(INTENT, LO[0], LO[1], won=1.0)       # LO wins once → its win-rate is > 0 (a real, lower, value)
wr_hi = lb.arm_stats(INTENT)[HI]["winrate"]
wr_lo = lb.arm_stats(INTENT)[LO]["winrate"]
ck("arm_score == winrate × idle (pure value-reward, no cost arithmetic)", abs(lb.arm_score(INTENT, HI) - wr_hi) < 1e-9,
   extra=f"score={lb.arm_score(INTENT, HI)} wr={wr_hi}")
ck("the higher value win-rate outranks the lower — routing follows the learned, cost-aware reward",
   lb.arm_score(INTENT, HI) > lb.arm_score(INTENT, LO), extra=f"hi={wr_hi} lo={wr_lo}")
ck("arm_score takes NO realized-cost argument (cost lives in the judge, not the router)",
   lb.arm_score.__code__.co_argcount == 2, extra=f"argcount={lb.arm_score.__code__.co_argcount}")

# ── B. the VALUE judge PROMPT leverages the realized costs + the 70/30 guidance (agentic, not arithmetic) ─────────────
p = lb._value_judge_prompt("do the task", "answer-A-body", "answer-B-body", cost_a=0.0010, cost_b=0.0070)
ck("priced judge prompt shows ANSWER A's realized $/call", "$0.0010" in p, extra=p)
ck("priced judge prompt shows ANSWER B's realized $/call", "$0.0070" in p, extra=p)
ck("priced judge prompt states the quality weight (70%)", "70%" in p, extra=p)
ck("priced judge prompt states the cost weight (30%)", "30%" in p, extra=p)
ck("priced judge prompt frames the tradeoff as a JUDGEMENT, not arithmetic", "not arithmetic" in p.lower())
ck("priced judge prompt asks whether the quality gain is WORTH the extra cost", "worth its extra cost" in p)
ck("priced judge prompt includes BOTH answers whole (evidence not truncated)",
   "answer-A-body" in p and "answer-B-body" in p)

# ── C. cold (no costs) → a pure-quality prompt: no percentages, no cost line ──────────────────────────────────────────
pq = lb._value_judge_prompt("do the task", "answer-A-body", "answer-B-body")
ck("cold prompt carries no cost figures", "$" not in pq, extra=pq)
ck("cold prompt carries no quality/cost split", "%" not in pq, extra=pq)
ck("cold prompt still asks which answer is better (pure quality)", "better" in pq.lower())

# ── D. the 70/30 is CONFIG — dialling bandit_quality_weight changes the prompt's stated split (not a hand-picked const) ─
_o_cfg = config._cfg_get
try:
    config._cfg_get = lambda s, k, d=None: ("0.6" if (s == "advisor" and k == "bandit_quality_weight") else
                                            ("0.4" if (s == "advisor" and k == "bandit_cost_weight") else _o_cfg(s, k, d)))
    p60 = lb._value_judge_prompt("t", "a", "b", cost_a=0.001, cost_b=0.007)
    ck("dialling bandit_quality_weight to 0.6 → the prompt states 60% / 40%", "60%" in p60 and "40%" in p60, extra=p60)
finally:
    config._cfg_get = _o_cfg

# ── E. _intent_realized_costs prices a higher-output (reasoning) arm above a lean one (the reasoning tax is SEEN) ──────
import spendguard.calls as _calls  # noqa: E402
import spendguard.lane_catalog as _lc  # noqa: E402
_o_cost = (_calls.mean_out_by_executor_model, _lc.use_name_cost, _lc.parse_use_name, _lc.quirk)
try:
    _calls.mean_out_by_executor_model = lambda intent: {("codex", "m-lean"): {"mean_out": 200, "n": 5},
                                                        ("codex", "m-reason"): {"mean_out": 1400, "n": 5}}
    _lc.parse_use_name = lambda un, lane: (un, None)
    _lc.quirk = lambda lane: {"style": "plain"}
    _lc.use_name_cost = lambda un, in_t, out_t, lane: 1e-5 * (in_t + out_t)
    costs = lb._intent_realized_costs("x", [("codex", "m-lean"), ("codex", "m-reason")])
    ck("_intent_realized_costs prices the higher-output (reasoning) arm above the lean arm",
       costs.get(("codex", "m-reason"), 0) > costs.get(("codex", "m-lean"), 0) > 0, extra=repr(costs))
finally:
    _calls.mean_out_by_executor_model, _lc.use_name_cost, _lc.parse_use_name, _lc.quirk = _o_cost

# ── F. run_bakeoff computes the realized costs and HANDS them to the value judge (the wiring) ─────────────────────────
A_LEAN, A_REASON = ("codex", "lean"), ("codex", "reason")
captured = {}
_o_wire = (lb._run_arm, lb._intent_realized_costs, lb.bakeoff_judge, lb._arm_cooling, _lc.arms, lb.arm_stats)
try:
    _lc.arms = lambda *a, **k: [A_LEAN, A_REASON]
    lb._arm_cooling = lambda *a, **k: False
    lb.arm_stats = lambda intent: {}                 # both untried → both are the least-tried pair
    lb._run_arm = lambda arm, *a, **k: (("LEAN-OUT" if arm == A_LEAN else "REASON-OUT"), 0)   # (text, out_tok)
    lb._intent_realized_costs = lambda intent, arms: {A_LEAN: 0.001, A_REASON: 0.007}

    def _capture_judge(task, oa, ob, aa, ab, ca=None, cb=None):
        captured["ca"], captured["cb"] = ca, cb
        return aa, "A"
    lb.bakeoff_judge = _capture_judge
    lb.run_bakeoff("x", "the task")
    ck("run_bakeoff hands the judge arm A's realized cost", captured.get("ca") == 0.001, extra=repr(captured))
    ck("run_bakeoff hands the judge arm B's realized cost", captured.get("cb") == 0.007, extra=repr(captured))
finally:
    lb._run_arm, lb._intent_realized_costs, lb.bakeoff_judge, lb._arm_cooling, _lc.arms, lb.arm_stats = _o_wire

print(f"\n{'OK' if not ck.fails else 'FAIL'} test_bandit_value_judge_weighs_cost: {len(ck.fails)} failure(s)")
sys.exit(1 if ck.fails else 0)
