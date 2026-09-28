"""Clusters B and C (caller-intent analysis, refute-confirmed 2026-09-28):

  B (measurement-identity integrity) — a caller that NAMES a model and RECORDS/REPORTS it as the model of record (a
     recommender, a ranker, a judge whose id is stored proposed_by=…) must pass no_substitution=True to adapters.call,
     or the utilisation bandit can silently serve a DIFFERENT model while the result claims the named one produced it.

  C (pricing correctness) — a caller of pricing.realtime_cost that KNOWS the provider must pass provider=…, or a bare
     model id hosted by several vendors (pricing._vendor_qualified) prices to the WRONG vendor. This bites hardest on
     the RECORDED cost paths (adapters._call_once, gate._rt_account) — the ledger's actual $, not just an estimate.

This guard AST-scans the named functions and asserts the required kwarg is present on the relevant call, so neither fix
can silently regress. Structural ($0, AST — parsing, not a judgement). Reads source only; no spendguard import.
"""
import sys, ast, pathlib

_SRC = pathlib.Path(__file__).resolve().parent.parent / "src" / "spendguard"

# B: (file, enclosing-func, callee-name, required kw=True). The callee is matched by its bare attribute/name.
_B_PINS = [
    ("advisor.py", "optimize", "call", "no_substitution"),          # reports model=model (recommender of record)
    ("advisor.py", "recommend_models", "call", "no_substitution"),  # reports model=model (ranker of record)
    ("advisor.py", "reconstruct", "call", "no_substitution"),       # records set_quality(src="judge"), reports model=judge
    ("lane_balance.py", "propose_substitutes", "call", "no_substitution"),  # records proposed_by=judge
]
# C: (file, enclosing-func, callee-name, required kw present — any value). realtime_cost must receive provider=.
_C_PROVIDER = [
    ("gate.py", "_rt_precheck", "realtime_cost"),
    ("adapters.py", "_call_once", "realtime_cost"),
    ("experiment.py", "_call", "realtime_cost"),
]


def _func_node(tree, name):
    for n in ast.walk(tree):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name:
            return n
    return None


def _calls_to(node, callee):
    for n in ast.walk(node):
        if isinstance(n, ast.Call):
            f = n.func
            nm = f.attr if isinstance(f, ast.Attribute) else (f.id if isinstance(f, ast.Name) else None)
            if nm == callee:
                yield n


def _has_kw_true(call, key):
    return any(kw.arg == key and isinstance(kw.value, ast.Constant) and kw.value.value is True
              for kw in call.keywords)


def _has_kw(call, key):
    return any(kw.arg == key for kw in call.keywords)


def main():
    fails = []

    def _load(fname):
        p = _SRC / fname
        return ast.parse(p.read_text()) if p.exists() else None

    # ── B: no_substitution=True on the measurement-identity calls ──
    for fname, func, callee, kw in _B_PINS:
        tree = _load(fname)
        fn = _func_node(tree, func) if tree else None
        calls = list(_calls_to(fn, callee)) if fn else []
        ok = bool(calls) and any(_has_kw_true(c, kw) for c in calls)
        print(f"  [{'OK' if ok else 'FAIL'}] B  {fname}::{func} -> {callee}(..., {kw}=True)")
        if not ok:
            fails.append(f"B {fname}::{func}: adapters.call must pass {kw}=True (measurement identity must not be "
                         f"silently substituted)")

    # ── C: provider= on the realtime_cost calls in the named functions ──
    for fname, func, callee in _C_PROVIDER:
        tree = _load(fname)
        fn = _func_node(tree, func) if tree else None
        calls = list(_calls_to(fn, callee)) if fn else []
        ok = bool(calls) and any(_has_kw(c, "provider") for c in calls)
        print(f"  [{'OK' if ok else 'FAIL'}] C  {fname}::{func} -> {callee}(..., provider=…)")
        if not ok:
            fails.append(f"C {fname}::{func}: realtime_cost must receive provider= (a bare multi-vendor id prices to "
                         f"the wrong vendor without it)")

    print(f"\n{'[FAIL]' if fails else '[OK]'} test_measurement_paths_pin_model_and_provider: {len(fails)} failure(s)")
    for f in fails:
        print("   -", f)
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
