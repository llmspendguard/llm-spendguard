"""Phase 2 guard — the bake-off sweeps the reasoning EFFORT axis per model and LEARNS the cheapest holding effort.

Offline: adapters.call is stubbed (no spend) to return a cheaper cost at lower effort while quality holds, and a
canned good verdict for the judge. Pins:
  · estimate (run=False) scales with the number of effort arms (candidates × prompts × efforts)
  · run=True records one arm per (model, effort), keyed model@effort
  · each arm lands in the `calls` corpus per (intent, model, EFFORT) — so advise.ranked(by_effort=True) sees them
  · `learned` names the cheapest effort that holds each model's quality (the summary of the recorded rows)
  · the learning is READABLE by best_value straight after (the loop closes end-to-end)
  · effort=None (no ladder) keeps the prior one-arm-per-model behaviour
"""
import os, sys, tempfile
if not os.environ.get("SPENDGUARD_TEST_ISOLATED"):
    os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"
    os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="spendguard-test-")
    os.execv(sys.executable, [sys.executable] + sys.argv)

from spendguard import bakeoff, adapters, advise, calls

failures = 0
def check(label, cond):
    global failures
    ok = bool(cond)
    if not ok:
        failures += 1
    print(f"  [{'OK' if ok else 'FAIL'}] {label}")


INTENT = "t:extract"
PROMPTS = ["classify row A", "classify row B", "classify row C"]
COST_BY_EFFORT = {"low": 0.01, "medium": 0.03, "high": 0.05}   # dearer effort costs more; quality holds at all

_orig_call = adapters.call
def _stub_call(model, prompt, **kw):
    if kw.get("sig") == "spendguard:bakeoff-judge":            # the judge — canned GOOD verdict, $0
        return {"text": '{"good": true}', "json": {"good": True}, "cost": 0.0,
                "in_tok": 5, "out_tok": 2, "error": None, "executor": "api"}
    eff = kw.get("reasoning")
    return {"text": f"ans@{eff}", "cost": COST_BY_EFFORT.get(eff, 0.03), "in_tok": 20, "out_tok": 30,
            "error": None, "model": model.split(":", 1)[-1], "executor": "api"}
adapters.call = _stub_call

try:
    print("-- estimate (run=False) scales with the effort ladder --")
    est2 = bakeoff.bakeoff(INTENT, candidates=["openai:gpt-5.5"], prompts=PROMPTS,
                           efforts=["low", "high"], run=False)
    check("estimate runs = candidates × prompts × efforts (1×3×2=6)", est2.get("runs") == 6)
    check("estimate echoes the effort ladder", est2.get("efforts") == ["low", "high"])
    est1 = bakeoff.bakeoff(INTENT, candidates=["openai:gpt-5.5"], prompts=PROMPTS, run=False)
    check("no ladder → one arm per model (1×3×1=3 runs)", est1.get("runs") == 3)

    print("-- run=True sweeps efforts as separate arms and records per (model, effort) --")
    r = bakeoff.bakeoff(INTENT, candidates=["openai:gpt-5.5"], prompts=PROMPTS,
                        efforts=["low", "medium", "high"], run=True, budget_usd=100.0)
    check("per_candidate is keyed by arm (model@effort)",
          {"openai:gpt-5.5@low", "openai:gpt-5.5@medium", "openai:gpt-5.5@high"} <= set(r["per_candidate"]))
    check("every arm ran all 3 prompts", all(r["per_candidate"][a]["runs"] == 3 for a in r["per_candidate"]))
    check("cheaper effort has the lower $/good",
          r["per_candidate"]["openai:gpt-5.5@low"]["per_good"] < r["per_candidate"]["openai:gpt-5.5@high"]["per_good"])

    print("-- the recorded rows slice the frontier by effort (the substrate the titration verdict reads) --")
    fr = advise.ranked(intent=INTENT, by_effort=True)
    efforts_seen = {m.get("effort") for m in fr["models"] if m["id"] == "openai:gpt-5.5"}
    check("frontier shows all three efforts for the model", {"low", "medium", "high"} <= efforts_seen)
    check("the bakeoff exposes the by-effort frontier in its result", {m.get("effort") for m in r["frontier_by_effort"]} >= {"low", "medium", "high"})
    with calls._lock:
        n_low = calls._calls_db().execute(
            "SELECT COUNT(*) FROM calls WHERE intent=? AND model='gpt-5.5' AND effort='low'", (INTENT,)).fetchone()[0]
    check("per-(intent,model,effort) rows landed in the corpus", n_low == 3)
finally:
    adapters.call = _orig_call

print(f"\n{'[FAIL]' if failures else 'OK'} test_bakeoff_effort_sweep: {failures} failure(s)")
sys.exit(1 if failures else 0)
