"""Offline test for living-insights scrub + the model-token matcher — NO db, NO network.

Locks two bug-prone bits: (1) sharing must scrub identity ($ amounts, intent names) while KEEPING the
generalizable rule; (2) model matching must be token-bounded so 'gpt-5' doesn't match inside 'gpt-5.5'
(that bug falsely superseded every insight).
"""
from spendguard import share, validate


def check(name, cond):
    print(f"  [{'OK' if cond else 'FAIL'}] {name}")
    assert cond


print("-- share.scrub (abstract, don't just delete) --")
ins = {"task_class": "classification", "regime": "bulk", "output_shape": "short-structured",
       "intent": "phase_taxonomy", "confidence": 0.8, "quality_basis": "unverified",
       "lesson": "phase_taxonomy on gpt-5.5 cost $1127 (~$49/job); gpt-4o-mini 26x cheaper",
       "action": "THEN use gpt-4o-mini not gpt-5.5 for phase_taxonomy", "condition": "IF bulk classify",
       "mechanism": "BECAUSE 2.50/15.00 input dominates"}
s = share.scrub(ins)
blob = (s["lesson"] + " " + s["action"] + " " + s["mechanism"])
check("returns a rule (has applicability context)", s is not None)
check("$ amounts scrubbed", "$1127" not in blob and "$49" not in blob)
check("price ratio basis scrubbed", "2.50/15.00" not in blob)
check("intent name scrubbed", "phase_taxonomy" not in blob)
check("model names KEPT (generalizable)", "gpt-4o-mini" in blob and "gpt-5.5" in blob)
check("ratio KEPT (generalizable)", "26x" in blob)
check("task context KEPT", s["task_class"] == "classification" and s["regime"] == "bulk")
check("bare sentence (no context) is NOT shareable", share.scrub({"lesson": "things cost money"}) is None)

print("-- validate._models_in (token-bounded, not substring) --")
known = ["gpt-5", "gpt-5.5", "gpt-5-nano", "claude-opus-4-8", "gpt-4o-mini"]
m1 = validate._models_in("reserve gpt-5.5; nano did it 26x cheaper", known)
check("gpt-5.5 matched", "gpt-5.5" in m1)
check("bare gpt-5 NOT matched inside gpt-5.5", "gpt-5" not in m1)
m2 = validate._models_in("use gpt-5-nano not claude-opus-4-8", known)
check("gpt-5-nano + claude-opus-4-8 matched", "gpt-5-nano" in m2 and "claude-opus-4-8" in m2)
check("gpt-5 still not matched in gpt-5-nano", "gpt-5" not in m2)
print("done.")
