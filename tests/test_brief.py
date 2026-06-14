"""Offline test for brief's pure helpers — slug + scale-from-task. NO db/network."""
from spendguard import brief


def check(name, cond):
    print(f"  [{'OK' if cond else 'FAIL'}] {name}")
    assert cond


print("-- _slug --")
check("slugifies + truncates", brief._slug("Re-type the NEW RxNorm codes!") == "re_type_the_new_rxnorm_codes")
check("empty → 'task'", brief._slug("") == "task")

print("-- _scale_default (pull a count from the task text) --")
check("extracts 40000 from task", "40000" in brief._scale_default("summarize 40000 notes", {}))
check("no number + no history → asks", "how many" in brief._scale_default("do the thing", {}))
check("no number + history → historical jobs", "12" in brief._scale_default("do it", {"primary": {"jobs": 12}}))
print("done.")
