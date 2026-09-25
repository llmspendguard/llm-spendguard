"""Warden 2026-09-25 (issues #2/#3): a bakeoff runs UNDER the caller's ambient `calls.context(intent=…)`, and the
quality JUDGE is spendguard's OWN ruler call — it must be attributed to a spendguard:* META intent (so it is
meta-capped and NEVER inflates the caller's workload intent). Passing sig="spendguard:bakeoff-judge" is NOT enough:
inside an ambient context, adapters.call does not override the intent, so the judge's $ would land under the caller's
intent (the attribution split the warden saw — some rows under the real intent, some under the bakeoff's). The fix
wraps the judge in an explicit nested context, exactly as requirement_judge._meta_call already does. Offline: adapters
is stubbed; no network, no model call."""
import os
import sys
import tempfile

os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="sg-judgemeta-")
os.environ.setdefault("SPENDGUARD_TEST_ISOLATED", "1")
os.environ.setdefault("SPENDGUARD_NO_AUTOINSTALL", "1")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from spendguard import bakeoff, calls, adapters, requirement_judge  # noqa: E402

_fails = []
def ck(label, cond):
    if not cond:
        _fails.append(label)
    print(f"  [{'OK' if cond else 'FAIL'}] {label}")

_seen = {}
_real_call = adapters.call
def _fake_call(model, prompt, **kw):
    _seen["intent"] = (calls.current() or {}).get("intent")   # the EFFECTIVE attribution intent at call time
    _seen["sig"] = kw.get("sig")
    _seen["no_sub"] = kw.get("no_substitution")
    return {"text": '{"good": true}', "parsed": {"good": True}, "cost": 0.0, "out_tok": 3, "in_tok": 10}


adapters.call = _fake_call
try:
    print("-- the bakeoff judge is META-attributed even inside the caller's context --")
    with calls.context(intent="warden:triage_bakeoff"):        # the caller's ambient context (a bakeoff runs under one)
        bakeoff._judge_one("a prompt", "an output", "claude-haiku-4-5")
    ck("the judge ran under a spendguard:* META intent (not the caller's)",
       _seen.get("intent") == "spendguard:bakeoff-judge")
    ck("the caller's workload intent did NOT absorb the judge's spend",
       _seen.get("intent") != "warden:triage_bakeoff")
    ck("the judge still PINS the model (no_substitution — a comparable ruler)", _seen.get("no_sub") is True)

    print("-- requirement_judge's ruler calls are meta-attributed too (already correct — kept honest) --")
    _seen.clear()
    with calls.context(intent="warden:triage_bakeoff"):
        requirement_judge._meta_call("claude-haiku-4-5", "q", system="s", schema={"type": "object"}, out=64, sig="req-screen")
    ck("requirement_judge screen call is under a spendguard:* META intent",
       (_seen.get("intent") or "").startswith("spendguard:") and _seen.get("intent") != "warden:triage_bakeoff")
finally:
    adapters.call = _real_call

print(f"\n{'[FAIL]' if _fails else 'OK'} test_bakeoff_judge_meta_intent: {len(_fails)} failure(s)")
sys.exit(1 if _fails else 0)
