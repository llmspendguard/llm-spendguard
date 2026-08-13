"""A COST ESTIMATE MAY NOT BE BUILT FROM INVENTED TOKEN COUNTS.

THE DEFECT THIS EXISTS TO STOP. On 2026-08-12 a sweep was quoted at $22.86–$34.29 and measured out at
~$380 — 17x — because the quote was:

    est = pricing.realtime_cost(model, 700, 160)

Both numbers were made up. Nothing about the output said so: it printed a dollar range, in the same format
as an estimate built from real measurements, and the run was authorized on it. The model in question was a
REASONING model, whose hidden thinking is billed as output, so the true figure was ~4,000 output tokens per
call and no assumed number could have seen it.

This is the same defect as the low `max_tokens` cap, one level up: a number nobody measured, sitting in a
literal, producing a plausible wrong answer that nothing contradicts. And as with that one, the repo already
had the remedy — the spend protocol says run a small test, MEASURE real tokens, then project, and
estimate.py implements it as `--from-sample`. The method was never wrong. It simply was not used at the one
place that authorized the spend.

WHAT IS CHECKED. Every call to a cost function anywhere in src/ or scripts/, looking for integer LITERALS in
the token-count positions. A literal there means the estimate was asserted rather than measured.

WHAT IS ALLOWED. Variables, measured values, fields off a usage object, sampled counts — anything that came
from somewhere. The rule is about provenance, not about arithmetic: `realtime_cost(m, in_tok, out_tok)` is
exactly right when those came from a real call.
"""
import ast
import pathlib
import sys

REPO = pathlib.Path(__file__).resolve().parents[1]

# The functions that turn token counts into money. A literal reaching any of them is a quoted price nobody
# measured. Named here so adding a new cost function is one edit rather than a new blind spot.
COST_FUNCS = {"realtime_cost", "batch_cost", "_cost", "estimate_jsonl_cost", "cost_of"}

# Token-count parameter positions/names on those functions.
TOKEN_ARGS = {"in_tok", "out_tok", "cached_in_tok", "avg_in", "avg_out", "prompt_tokens", "completion_tokens"}

SKIP_DIRS = {".venv", ".venv.nosync", "__pycache__", "build", "dist", ".git"}

_fails = []


def check(name, cond, detail=""):
    print(f"  [{'OK' if cond else 'FAIL'}] {name}" + (f"\n        {detail}" if detail and not cond else ""))
    if not cond:
        _fails.append(name)


def offenders(roots):
    out = []
    for root in roots:
        base = REPO / root
        if not base.exists():
            continue
        for p in sorted(base.rglob("*.py")):
            if any(x in p.parts for x in SKIP_DIRS):
                continue
            try:
                tree = ast.parse(p.read_text(errors="ignore"))
            except SyntaxError:
                continue
            for n in ast.walk(tree):
                if not isinstance(n, ast.Call):
                    continue
                fn = n.func.attr if isinstance(n.func, ast.Attribute) else getattr(n.func, "id", "")
                if fn not in COST_FUNCS:
                    continue
                # positional token args: everything after the model argument
                lits = [a.value for a in n.args[1:]
                        if isinstance(a, ast.Constant) and isinstance(a.value, int)
                        and not isinstance(a.value, bool)]
                lits += [k.value.value for k in n.keywords
                         if k.arg in TOKEN_ARGS and isinstance(k.value, ast.Constant)
                         and isinstance(k.value.value, int) and not isinstance(k.value.value, bool)]
                # 0 is not an invented measurement — it is the explicit absence of tokens (an embedding
                # call has no output). Only NON-ZERO literals assert a quantity nobody counted.
                lits = [v for v in lits if v]
                if lits:
                    out.append((str(p.relative_to(REPO)), n.lineno, fn, lits))
    return out


print("-- a quoted price must come from a measurement, not a literal --")
# THE VERDICT IS AGENTIC AND RECORDED; THIS READS IT. The first version of this check flagged EVERY cost
# call carrying an int literal, and it was wrong about three of five: `bool(realtime_cost(m, 1000, 1000))`
# asks whether a model has a published price — the numbers are arbitrary and the result is never shown as
# money. Syntax cannot separate that from a real quote, because the difference is what the result is USED
# for. A guard that cries wolf on three of five gets switched off, taking the two real ones with it.
#
# So `spendguard estimate-literals --judge` has a model rule on each site, the verdict is committed, and
# this test reads it — offline, deterministic, and failing on any site with NO verdict.
from spendguard import estimate_literals  # noqa: E402

_res = estimate_literals.unruled_and_quotes(REPO)
check(f"all {_res['total']} literal-fed cost call(s) have a recorded verdict", not _res["unjudged"],
      "UNJUDGED (run `spendguard estimate-literals --judge`): "
      + "; ".join(f"{s['file']}:{s['symbol']} {s['fn']}{tuple(s['literals'])}" for s in _res["unjudged"]))
check("no QUOTED PRICE is built from invented token counts", not _res["failed"],
      "; ".join(f"{q['file']}:{q['symbol']} {q['fn']}{tuple(q['literals'])} — {q.get('why','')}"
                for q in _res["failed"])
      + "  — rebuild from a measured sample (estimate.fit_from_sample / expected_output.expect). "
        "A literal here is how $34 was quoted for a $380 run.")

# The remedy must also be REACHABLE, not merely documented. A protocol nobody can call is the same as no
# protocol — which is exactly how this defect survived: the method existed in estimate.py and the code that
# needed it built its own literal instead.
from spendguard import estimate  # noqa: E402

check("the measure-then-project path exists and is importable",
      callable(getattr(estimate, "fit_from_sample", None)),
      "estimate.fit_from_sample is the sanctioned way to turn a real sample into a projection")

print("\nPASS — 0 failure(s)" if not _fails else f"\nFAIL — {len(_fails)} failure(s): " + "; ".join(_fails))
sys.exit(1 if _fails else 0)
