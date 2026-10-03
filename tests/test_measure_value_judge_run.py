"""The A2.1 value-judge measurement RUN HARNESS (scripts/probe/measure_value_judge_run.py) — guards the two safety
properties that the pilot MALFUNCTION (a ~1100-call fan-out) and the API-spend protocol require, offline + $0:

  1. HARD-CAGE: every judge/grade call reaches adapters.call with metered_only=True + reasoning='minimal' +
     no_substitution=True — one metered, billable, UN-ROUTABLE call (no bandit/best-value fan-out). This is the fix
     for the exact malfunction, so it must be un-regressable.
  2. REAL-LEDGER-DELTA CAP: the run reads budget.spent_by_job(run_chain) (the measurement's own recorded billed $)
     and ABORTS the instant it meets/exceeds --budget — never estimated-unit × count.
  3. ACCOUNTING: 3 caged calls (before judge + after judge + grade... grade is 2 here, lean+heavy) + 2 arm-gen per
     pair; the before/after picks are tallied from the judge replies.
  4. arms DERIVED from recorded data (lowest vs highest mean output); _pick parses A/B/TIE.

Nothing spends: adapters.call, lane_bandit._run_arm, the norms, and the review-task source are stubbed."""
import importlib.util
import os
import sys
import tempfile

os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="sg-measurerun-")
os.environ.setdefault("SPENDGUARD_TEST_ISOLATED", "1")
os.environ.setdefault("SPENDGUARD_NO_AUTOINSTALL", "1")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

import spendguard  # noqa: E402
spendguard.require = lambda: None                      # bypass the fail-closed gate FOR THIS OFFLINE TEST only
from spendguard import calls, lane_bandit, budget, adapters  # noqa: E402

# load the harness script as a module
_HPATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts", "probe", "measure_value_judge_run.py")
_spec = importlib.util.spec_from_file_location("measure_value_judge_run", _HPATH)
mr = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mr)

fails = []


def ck(name, cond, extra=""):
    print(("  [OK] " if cond else "  [FAIL] ") + name + (("  — " + extra) if extra and not cond else ""))
    if not cond:
        fails.append(name)


LEAN, HEAVY = ("codex", "gpt-5.6-sol"), ("zai-coding", "glm-5.3")

print("-- arms DERIVED from recorded output (lowest mean = lean, highest = heavy) --")
calls.mean_out_by_executor_model = lambda intent: {LEAN: {"mean_out": 13.0, "n": 5}, HEAVY: {"mean_out": 510.0, "n": 5}}
arms = mr._derive_arms("t")
ck("lean arm = the low-output one", arms and arms["lean"]["arm"] == LEAN and arms["heavy"]["arm"] == HEAVY)

print("-- _pick EXACT-matches the instructed one-word reply (A=lean, B=heavy, TIE=tie); off-format → unparseable --")
ck("'A' → lean", mr._pick("A") == "lean")
ck("'B.' → heavy (only trailing punctuation stripped)", mr._pick("B.") == "heavy")
ck("'TIE' → tie", mr._pick("TIE") == "tie")
ck("off-format 'Actually, B' → unparseable (never a prefix guess)", mr._pick("Actually, B") == "unparseable")

print("-- HARD-CAGE: every caged call reaches adapters.call metered_only + reasoning='minimal' + no_substitution --")
_wire = []
adapters.call = lambda model, prompt, **kw: (_wire.append(kw) or {"text": "A", "error": None})
lane_bandit._bandit_judge_model = lambda: "haiku-test"
mr._caged_call("judge me", "chain-x")
ck("metered_only=True (forces the metered model, no $0-lane swap)", _wire and _wire[-1].get("metered_only") is True)
ck("reasoning='minimal' (pinned — no best-value routing)", _wire[-1].get("reasoning") == "minimal")
ck("no_substitution=True (no bandit swap of the model)", _wire[-1].get("no_substitution") is True)

lane_bandit.estimate_judge_cost = lambda bakeoffs=(10, 100, 1000): {"per_bakeoff_usd": 0.004, "judge_model": "haiku-test",
                                                                    "in_tok_bound": 100, "out_tok_cap": 200, "monthly": {}}

print("-- REAL-LEDGER-DELTA CAP: run STOPS before a pair when spent + the pair's worst-case cost would exceed budget --")
budget.spent_by_job = lambda chain, since=None: 999.0           # pretend the ledger already shows $999 for this run
mr._review_tasks = lambda M, src=None: ["task-%d" % i for i in range(M)]
lane_bandit._run_arm = lambda arm, *a, **k: ("OUT-%s" % arm[0], 10)
lane_bandit._intent_realized_costs = lambda intent, arms2: {LEAN: 0.001, HEAVY: 0.007}
res = mr.run("t", 3, budget_usd=0.10)
ck("cap exceeded up front → 0 pairs measured, aborted", res["pairs_done"] == 0 and res["aborted"] is True)

print("-- ACCOUNTING: under the cap, M pairs each do 2 arm-gen + 4 caged calls (before+after judge + 2 grades) --")
_wire.clear()
_arm_calls = []
lane_bandit._run_arm = lambda arm, *a, **k: (_arm_calls.append(arm) or ("OUT-%s" % arm[0], 10))
# before judge → 'A' (lean), after judge → 'B' (heavy), grades → '7'. Sequence per pair: before,after,grade_lean,grade_heavy.
_seq = iter(["A", "B", "7", "7"] * 2)
adapters.call = lambda model, prompt, **kw: (_wire.append(kw) or {"text": next(_seq), "error": None})
budget.spent_by_job = lambda chain, since=None: 0.0            # under the cap throughout
res2 = mr.run("t", 2, budget_usd=100.0)
ck("2 pairs measured", res2["pairs_done"] == 2)
ck("4 caged calls per pair (before+after judge + lean+heavy grade) = 8", len(_wire) == 8, extra="wire=%d" % len(_wire))
ck("2 arm-gen per pair = 4 (deliberate _run_arm, never routed)", len(_arm_calls) == 4, extra="arm=%d" % len(_arm_calls))
ck("EVERY caged call was metered_only+pinned+no_substitution (no un-caged judge anywhere)",
   all(w.get("metered_only") and w.get("reasoning") == "minimal" and w.get("no_substitution") for w in _wire))
ck("before picks lean (pure quality 'A'), after picks heavy ('B') — tallied from the replies",
   res2["before"]["lean"] == 2 and res2["after"]["heavy"] == 2)

print(f"\n{'OK' if not fails else 'FAIL'} test_measure_value_judge_run: {len(fails)} failure(s)")
sys.exit(1 if fails else 0)
