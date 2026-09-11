"""Guard — the two-tier REQUIREMENT judge (requirement_judge): screen with a cheap model, escalate to an OPUS
adjudicator ONLY when the screen says it is not confident (an agentic self-assessment, not a code threshold).

Pins:
  · requirements_for extracts the prompt's criteria and CACHES per prompt-hash (extracted once per distinct prompt);
  · a CONFIDENT screen verdict stands — the adjudicator is NOT called (tier='screen');
  · a NOT-confident screen ESCALATES — the adjudicator rules, its verdict stands, confident=True (tier='adjudicated');
  · the WHOLE output is sent to the judge (no pre-truncation — a bad suffix must be visible);
  · an unavailable screen → good=None (UNLABELED), never a guessed label.
Offline: adapters.call is stubbed and dispatches on the sig — no network, no spend.
"""
import os, sys, tempfile, json
if not os.environ.get("SPENDGUARD_TEST_ISOLATED"):
    os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"
    os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="spendguard-test-")
    _self = os.path.realpath(__file__)
    _root = os.path.realpath(os.path.dirname(__file__)) + os.sep
    if not _self.startswith(_root):
        raise SystemExit("refusing to re-exec a path outside the test directory: %s" % _self)
    os.execv(sys.executable, [sys.executable, _self])

from spendguard import requirement_judge, adapters

_fails = []
def check(label, cond):
    if not cond:
        _fails.append(label)
    print(f"  [{'OK' if cond else 'FAIL'}] {label}")


# ── controllable stub: counts calls per sig, and lets the test flip the screen's confidence ──
_calls = {"requirement-extract": 0, "requirement-screen": 0, "requirement-adjudicate": 0}
_screen_confident = {"v": True}
_adj_fail = {"v": False}
_seen_prompts = {"screen": None}
_orig = adapters.call
def _stub(model, prompt, max_tokens=None, system=None, schema=None, sig=None, timeout_s=None, **kw):
    tag = (sig or "").split(":", 1)[-1]
    _calls[tag] = _calls.get(tag, 0) + 1
    if tag == "requirement-extract":
        j = {"requirements": ["output must be valid JSON", "must state a numeric score"]}
    elif tag == "requirement-screen":
        _seen_prompts["screen"] = prompt
        j = {"good": True, "usable": True, "score": 7, "confident": _screen_confident["v"],
             "requirements_met": [{"requirement": "output must be valid JSON", "met": True},
                                  {"requirement": "must state a numeric score", "met": False}]}
    elif tag == "requirement-adjudicate":
        if _adj_fail["v"]:
            return {"error": "adjudicator down", "text": None, "cost": 0.0}
        j = {"good": True, "usable": True, "score": 9, "confident": True,
             "requirements_met": [{"requirement": "output must be valid JSON", "met": True},
                                  {"requirement": "must state a numeric score", "met": True}]}
    else:
        return {"error": "unexpected sig %r" % sig, "text": None, "cost": 0.0}
    return {"json": j, "text": json.dumps(j), "error": None, "cost": 0.001}
adapters.call = _stub

try:
    requirement_judge._req_cache.clear()
    PROMPT = "Grade the essay and return JSON with a score."
    OUTPUT = "prefix ok ... " + ("X" * 500) + " ... FINAL: {\"score\": 4}"

    print("-- requirements_for extracts + CACHES per prompt (LLM called once) --")
    r1 = requirement_judge.requirements_for(PROMPT)
    r2 = requirement_judge.requirements_for(PROMPT)
    check("extraction returned the criteria", r1 == ["output must be valid JSON", "must state a numeric score"])
    check("second call HIT the cache (extract LLM called once)", _calls["requirement-extract"] == 1 and r2 == r1)

    print("-- a CONFIDENT screen stands; the adjudicator is NOT called --")
    _screen_confident["v"] = True
    v = requirement_judge.judge_requirements(PROMPT, OUTPUT)
    check("tier is 'screen'", v["tier"] == "screen")
    check("adjudicator was NOT called", _calls["requirement-adjudicate"] == 0)
    check("failed requirement surfaced (typed, from the judge)", v["failed"] == ["must state a numeric score"])
    check("the WHOLE output reached the judge (no truncation of the bad suffix)", OUTPUT in (_seen_prompts["screen"] or ""))

    print("-- a NOT-confident screen ESCALATES to the opus adjudicator --")
    v2 = requirement_judge.judge_requirements(PROMPT, OUTPUT)   # cached requirements → no new extract
    check("no extra extraction (requirements cached)", _calls["requirement-extract"] == 1)
    _screen_confident["v"] = False
    v3 = requirement_judge.judge_requirements(PROMPT, OUTPUT + " (variant)")
    check("adjudicator WAS called on the not-confident screen", _calls["requirement-adjudicate"] == 1)
    check("the adjudicated verdict stands (tier=adjudicated; the adjudicator's OWN confidence preserved)",
          v3["tier"] == "adjudicated" and v3["confident"] is True)
    check("the adjudicator's score is the one returned", v3["score"] == 9)

    print("-- screen NOT confident + adjudicator UNAVAILABLE → UNLABELED (not the screen's uncertain guess) --")
    _screen_confident["v"] = False
    _adj_fail["v"] = True
    v_unres = requirement_judge.judge_requirements(PROMPT, OUTPUT + " (adj-down)")
    check("good is None (unresolved — not the screen's guess)", v_unres["good"] is None)
    check("score is None (titration won't record it either)", v_unres["score"] is None)
    _adj_fail["v"] = False

    print("-- an unavailable screen → good=None (UNLABELED), never guessed --")
    adapters.call = lambda *a, **k: {"error": "boom", "text": None, "cost": 0.0}
    v4 = requirement_judge.judge_requirements(PROMPT, OUTPUT, requirements=["x"])
    check("good is None when the screen judge is unavailable", v4["good"] is None)
finally:
    adapters.call = _orig

print(f"\n{'[FAIL]' if _fails else 'OK'} test_requirement_judge: {len(_fails)} failure(s)")
sys.exit(1 if _fails else 0)
