"""lane_eligibility: the LANE-ELIGIBLE-BUT-METERED figure (defect 3) — of realtime metered spend, how much was
independent one-shot comprehension that could have ridden a $0 lane / the Batch API but paid the metered API. Offline:
the ledger aggregator and the agentic judge (prompts._batchable_verdict) are stubbed with DECLARED fixture data; the
per-intent verdict cache writes to an isolated temp HOME. Zero real spend.

Pins the contract: eligibility is an AGENTIC per-intent judgement (never a regex/prefix allowlist), the verdict is
CACHED so a $0 surface never re-pays, spend is OPT-IN (execute=True) and estimated first, and the figure folds across
models per intent."""
import os
import sys
import tempfile

if not os.environ.get("SPENDGUARD_TEST_ISOLATED"):
    os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"
    os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="spendguard-laneelig-")
    os.execv(sys.executable, [sys.executable] + sys.argv)

from spendguard import lane_eligibility as le, calls, prompts

fails = []


def ck(name, cond):
    print(("  [OK] " if cond else "  [FAIL] ") + name)
    if not cond:
        fails.append(name)


# Stub the ledger: two intents, one of them split across two models (must FOLD to one intent row).
_ROWS = [
    {"intent": "bulk-classify", "model": "gpt-6-sol", "calls": 3571, "usd": 124.56, "in_tok": 29422393, "avg_in": 8239},
    {"intent": "bulk-classify", "model": "claude-opus-4-8", "calls": 100, "usd": 10.00, "in_tok": 100000, "avg_in": 1000},
    {"intent": "chat-turn", "model": "gpt-6-sol", "calls": 50, "usd": 20.00, "in_tok": 50000, "avg_in": 1000},
]
calls.metered_realtime_by_intent = lambda since: list(_ROWS)
# Stub the AGENTIC judge with DECLARED fixture verdicts (data, not a decision rule): the real judge is an LLM; the
# test fixes its output per intent so the aggregation/caching/estimate logic can be asserted deterministically.
_FIXTURE_VERDICT = {"bulk-classify": True, "chat-turn": False}   # intent -> the judge's eligible verdict in this fixture
_judged = []
def _fake_verdict(intent, model, n, med_in):
    _judged.append(intent)
    return {"batchable": _FIXTURE_VERDICT[intent], "why": f"stub verdict for {intent}"}
prompts._batchable_verdict = _fake_verdict

# ── 1. execute=False with an empty cache: nothing judged yet, $0, and an honest estimate of the classify cost ──
rep0 = le.lane_eligible_report(execute=False)
ck("total metered is folded across models ($134.56 bulk + $20 chat = $154.56)", rep0["total_metered_usd"] == 154.56)
ck("nothing judged yet → eligible_usd 0", rep0["eligible_usd"] == 0.0)
ck("both intents counted UNJUDGED (folded to 2, not 3 rows)", rep0["unjudged"] == 2)
ck("unjudged $ is the full metered total until judged", rep0["unjudged_usd"] == 154.56)
ck("no judge call was made on the $0 estimate path", _judged == [])
ck("the classify-cost estimate is present (≥ 0, priced from pricing.py)", rep0["est_judge_usd"] >= 0)

# ── 2. execute=True: judge each intent ONCE (agentic), cache it, and sum the eligible $ ──
rep1 = le.lane_eligible_report(execute=True)
ck("eligible_usd = the independent-one-shot intent's folded $ (bulk-classify $134.56)", rep1["eligible_usd"] == 134.56)
ck("both intents judged", rep1["judged"] == 2 and rep1["unjudged"] == 0)
ck("eligible token/call totals fold across models (3571+100 calls)", rep1["eligible_calls"] == 3671)
ck("each distinct intent judged exactly once (folded, not per-row)", sorted(set(_judged)) == ["bulk-classify", "chat-turn"])
ck("the interactive intent is marked legit-metered, not eligible",
   any(d["intent"] == "chat-turn" and d["eligible"] is False for d in rep1["by_intent"]))

# ── 3. the verdict is CACHED — a second $0 report reproduces the figure with NO new judge call ──
_judged.clear()
rep2 = le.lane_eligible_report(execute=False)
ck("cached verdicts reproduce the eligible figure at $0", rep2["eligible_usd"] == 134.56 and rep2["unjudged"] == 0)
ck("no re-judging on the cached path (never re-pays)", _judged == [])

# ── 4. summary_line surfaces the figure (and would name unjudged coverage when present) ──
line = le.summary_line()
ck("summary_line names the eligible-of-total figure", line is not None and "$134.56 of $154.56" in line)

print(("[OK]" if not fails else "[FAIL]") + " lane eligibility: %d failure(s)" % len(fails))
sys.exit(1 if fails else 0)
