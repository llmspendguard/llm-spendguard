"""A failed bakeoff arm is COUNTED-and-SKIPPED, and a judge that RAISES cannot abort the slate. Guards the two gaps
behind the observed "one bad reply killed the other 11 requests" / "ValueError: no JSON object in the reply":
  · an arm call that errors OR returns empty text counts as `failed`, is skipped, and is never handed to the judge
  · a judge exception is contained — that run is UNLABELED (verdict None), the error is surfaced, the loop continues,
    and every arm already measured still lands.
Offline: adapters.call is stubbed (arm replies only) and _judge_one is stubbed to raise on one output; no spend."""
import os
import sys
import tempfile

if not os.environ.get("SPENDGUARD_TEST_ISOLATED"):
    os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"
    os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="sg-bakeoff-resil-")
    os.execv(sys.executable, [sys.executable] + sys.argv)

from spendguard import bakeoff, adapters  # noqa: E402

fails = []
def ck(label, cond):
    print(f"  [{'OK' if cond else 'FAIL'}] {label}")
    if not cond:
        fails.append(label)

def _stub_call(model, prompt, **kw):
    if prompt == "err":
        return {"text": None, "error": "boom http=400", "cost": 0.0, "executor": "api"}
    if prompt == "empty":
        return {"text": "   ", "error": None, "cost": 0.0, "in_tok": 5, "out_tok": 0, "executor": "api"}
    return {"text": f"ANS:{prompt}", "error": None, "cost": 0.01, "in_tok": 10, "out_tok": 5,
            "executor": "api", "model": model.split(":", 1)[-1]}
adapters.call = _stub_call

def _stub_judge(prompt, output, judge_model, **kw):
    if output and "judgeraise" in output:
        raise ValueError("no JSON object in the reply")     # the exact crash shape from the field report
    return True if (output and "good" in output) else None
bakeoff._judge_one = _stub_judge

r = bakeoff.bakeoff("t:resil", candidates=["openai:gpt-5.5"],
                    prompts=["err", "empty", "good", "judgeraise"], run=True, budget_usd=100.0)

ck("the bakeoff COMPLETED despite a bad arm + a raising judge (returned a result dict)",
   isinstance(r, dict) and "per_candidate" in r)
arm = r["per_candidate"]["openai:gpt-5.5"]
ck("the error arm AND the empty arm are both counted as failed (2)", arm["failed"] == 2)
ck("the two text-producing arms counted as runs (good + judgeraise)", arm["runs"] == 2)
ck("only the good output got a label — the judge-raise is UNLABELED, not a crash",
   arm["labeled"] == 1 and arm["good"] == 1)
ck("a judge error is surfaced in last_error (not swallowed silently)",
   "judge error" in (arm["last_error"] or ""))

print(f"\n{'[FAIL]' if fails else 'OK'} test_bakeoff_arm_resilience: {len(fails)} failure(s)")
sys.exit(1 if fails else 0)
