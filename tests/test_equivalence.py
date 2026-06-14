"""Offline test for the graded equivalence ladder — NO network (free tiers only)."""
from spendguard import equivalence as E


def check(name, cond):
    print(f"  [{'OK' if cond else 'FAIL'}] {name}")
    assert cond


print("-- grade() ladder (free tiers) --")
ref = '{"r":[["V","m",1],["R","h",0]]}'
s, t = E.grade(ref, ref)
check("identical → (1.0, exact)", s == 1.0 and t == "exact")
s, t = E.grade(ref, '{"r":[["V","m",1],["R","h",9]]}')
check("one field differs → (graded, scalar)", 0.5 < s < 1.0 and t == "scalar")
s, t = E.grade("the cat sat", "the cat sat on the mat")
check("prose → (ratio, text)", 0.0 < s < 1.0 and t == "text")

print("-- structural (format/contract preserved, values ignored) --")
check("same shape, different values → True",
      E.structural('{"r":[["V","m",1]]}', '{"r":[["X","y",9]]}') is True)
check("different shape (extra row) → False",
      E.structural('{"r":[[1]]}', '{"r":[[1],[2]]}') is False)
check("non-JSON → None", E.structural("hi", "yo") is None)
print("done.")
