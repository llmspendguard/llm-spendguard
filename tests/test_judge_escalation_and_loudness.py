"""GUARD — two robustness fixes so requirement-aware / generic judging can never silently label nothing:

  FIX A (escalation): a screen that CANNOT rule — either it self-reports not-confident OR it returns no parseable
  verdict at all — MUST escalate to the OPUS adjudicator. Returning UNLABELED on an unparseable screen without
  escalating (the old behavior) is the 'cannot tell is not clean' violation that let a lane's fenced reply label
  nothing while opus was never asked. Only a CONFIDENT, PARSED screen rules directly (stays cheap).

  FIX C (loudness): a bakeoff whose runs produced ZERO quality labels measured nothing; it must surface a LOUD
  `warning`, never a clean-looking result with good_rate=null everywhere.

Hermetic: the judge tiers / candidate calls / recorders are stubbed; no network, zero spend."""
import os
import sys
import tempfile

os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="spendguard-judgefix-")
os.environ.setdefault("SPENDGUARD_TEST_ISOLATED", "1")
os.environ.setdefault("SPENDGUARD_NO_AUTOINSTALL", "1")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from spendguard import requirement_judge   # noqa: E402

fails = []


def ck(name, cond):
    print(("  [OK] " if cond else "  [FAIL] ") + name)
    if not cond:
        fails.append(name)


# ───────────────────── FIX A: escalation on an unresolvable screen ─────────────────────
_EXTRACT = ({"requirements": ["r1"]}, 0.0)
_CONFIDENT = ({"good": True, "usable": True, "score": 9, "confident": True, "requirements_met": []}, 0.001)
_UNSURE = ({"good": True, "usable": True, "score": 6, "confident": False, "requirements_met": []}, 0.001)
_ADJ = ({"good": False, "usable": True, "score": 4, "confident": True, "requirements_met": []}, 0.01)


def _meta(screen_ret, adj_ret):
    seq = []

    def _fake(model, prompt, *, system, schema, out, sig):
        seq.append(sig)
        if sig == "requirement-extract":
            return _EXTRACT
        if sig == "requirement-screen":
            return screen_ret
        if sig == "requirement-adjudicate":
            return adj_ret
        return None, 0.0
    return _fake, seq


_orig_meta = requirement_judge._meta_call
print("-- FIX A: only a confident, parsed screen rules directly; everything else escalates to opus --")
try:
    # 1. CONFIDENT parsed screen → rules directly, opus NEVER called (the cheap path stays cheap)
    requirement_judge._meta_call, seq = _meta(_CONFIDENT, _ADJ)
    requirement_judge._req_cache.clear()
    v = requirement_judge.judge_requirements("p", "o")
    ck("a CONFIDENT screen rules on the screen tier (no needless opus call)",
       v.get("tier") == "screen" and "requirement-adjudicate" not in seq and v.get("good") is True)

    # 2. NOT-CONFIDENT parsed screen → escalates (pre-existing behavior, kept)
    requirement_judge._meta_call, seq = _meta(_UNSURE, _ADJ)
    requirement_judge._req_cache.clear()
    v = requirement_judge.judge_requirements("p", "o")
    ck("a NOT-CONFIDENT screen escalates to the adjudicator (tier=adjudicated)",
       "requirement-adjudicate" in seq and v.get("tier") == "adjudicated" and v.get("good") is False)

    # 3. UNPARSEABLE screen (None) → NOW escalates (the fix). Was: silent good=None, opus never asked.
    requirement_judge._meta_call, seq = _meta((None, 0.001), _ADJ)
    requirement_judge._req_cache.clear()
    v = requirement_judge.judge_requirements("p", "o")
    ck("an UNPARSEABLE screen ESCALATES to opus (the bug: it used to return good=None silently)",
       "requirement-adjudicate" in seq and v.get("tier") == "adjudicated" and v.get("good") is False)

    # 4. BOTH tiers unparseable → UNLABELED, but tier='unresolved' + a why naming both (never a silent None)
    requirement_judge._meta_call, seq = _meta((None, 0.001), (None, 0.01))
    requirement_judge._req_cache.clear()
    v = requirement_judge.judge_requirements("p", "o")
    ck("both tiers unavailable → good=None, tier='unresolved', why names the escalation attempt",
       v.get("good") is None and v.get("tier") == "unresolved" and "adjudicator unavailable" in (v.get("why") or ""))
finally:
    requirement_judge._meta_call = _orig_meta


# ───────────────────── FIX C: a bakeoff that labels 0/N is LOUD, not silently null ─────────────────────
print("\n-- FIX C: a bakeoff whose judge labels nothing surfaces a LOUD warning --")
from spendguard import bakeoff as bk, adapters as _ad, advise as _adv, measurement as _meas  # noqa: E402

_saved = (_ad.call, _ad.provider_for, bk._judge_one, bk.calls.insert, _adv.ranked, _meas.record_reading)
try:
    _ad.call = lambda c, p, **kw: {"text": "a candidate output", "cost": 0.0, "error": None, "in_tok": 5, "out_tok": 5}
    _ad.provider_for = lambda c: c.split(":", 1)[0]
    bk._judge_one = lambda p, o, jm: None                       # the JUDGE rules on nothing → 0 labels
    bk.calls.insert = lambda *a, **k: None
    _adv.ranked = lambda **k: {"models": [], "pick": None, "metric": "per_good"}
    _meas.record_reading = lambda **k: "reading-x"
    res = bk.bakeoff("soft-intent", candidates=["openai:gpt-5-nano"],
                     prompts=["write a nuanced, holistic summary"], run=True, budget_usd=999)
    ck("runs happened (candidate did not error)", sum(a["runs"] for a in res["per_candidate"].values()) > 0)
    ck("every arm's good_rate is null with labeled=0 (the judge labeled nothing)",
       all(a["labeled"] == 0 and a["good_rate"] is None for a in res["per_candidate"].values()))
    ck("the result carries a LOUD `warning` (not a silent clean-looking null)",
       bool(res.get("warning")) and "ZERO quality labels" in res["warning"])
finally:
    (_ad.call, _ad.provider_for, bk._judge_one, bk.calls.insert, _adv.ranked, _meas.record_reading) = _saved

print(f"\n{'[FAIL]' if fails else 'OK'} test_judge_escalation_and_loudness: {len(fails)} failure(s)")
sys.exit(1 if fails else 0)
