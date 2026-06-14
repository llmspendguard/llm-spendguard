"""Offline test for cache-audit pure logic — NO db, NO network."""
from spendguard import cacheaudit


def check(name, cond):
    print(f"  [{'OK' if cond else 'FAIL'}] {name}")
    assert cond


print("-- _common_prefix (the cacheable shared block) --")
shared = "You are a clinical informatics expert.\nClassify:\n"
prompts = [shared + "aspirin", shared + "metformin", shared + "lisinopril"]
cp = cacheaudit._common_prefix(prompts)
check("finds the shared system block", cp.startswith("You are a clinical informatics expert."))
check("trims to a clean newline boundary", cp.endswith("\n"))
check("no shared prefix → empty", cacheaudit._common_prefix(["abc", "xyz"]) == "")
check("single prompt handled", cacheaudit._common_prefix(["only one"]) == "only one")
print("done.")
