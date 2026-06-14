"""Offline test for the experiment lab's equivalence logic — NO network, NO db.

The equivalence metric must be GRADED (fraction of fields matching), not all-or-nothing — exact match
scores 0% on any rich nested output (even a model vs itself), which is uninformative.
"""
from spendguard import experiment


def check(name, cond):
    print(f"  [{'OK' if cond else 'FAIL'}] {name}")
    assert cond


print("-- _equiv (graded JSON equivalence) --")
ref = '{"r":[["V","m",1],["R","h",0]]}'
check("identical → 1.0", abs(experiment._equiv(ref, ref) - 1.0) < 1e-9)
# one of six scalars differs → ~0.83, NOT 0
partial = '{"r":[["V","m",1],["R","h",9]]}'
e = experiment._equiv(ref, partial)
check("one field differs → graded (not 0, not 1)", 0.5 < e < 1.0)
check("totally different → low", experiment._equiv(ref, '{"r":[["X","x",7],["Y","y",8]]}') < 0.4)
check("unparseable variant → falls back to string ratio (≤1)", 0.0 <= experiment._equiv(ref, "I refuse") <= 1.0)

print("-- _flatten (document-order scalars) --")
check("nested flattened in order", list(experiment._flatten([1, [2, 3], {"b": 4, "a": 5}])) == [1, 2, 3, 5, 4])
print("done.")
