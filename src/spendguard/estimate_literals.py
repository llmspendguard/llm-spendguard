"""Every cost function called with an integer literal, and a model's ruling on each.

WHY THIS IS NOT A LINT RULE. The obvious check is "a cost function must not receive an int literal", and
it is wrong. Of the nine sites found in this repo, two are not estimates at all:

    priced = bool(pricing.realtime_cost(m, 1000, 1000))     # "can this model be priced?"

The numbers are arbitrary non-zero inputs used to ask whether the price table answers for this model. A
third prints a per-1M worked example, where 1,000,000 is the UNIT the rates are quoted in. None of those is
a quoted price and none of them should be measured — but no amount of looking at the syntax can tell them
apart from the real defect, which looked like this:

    est = pricing.realtime_cost(model, 700, 160)            # a $34 quote for a ~$380 run

Same function, same shape, same literal ints. The difference is what the RESULT IS USED FOR, which is
meaning. A mechanical rule flags all five and a human turns it off; that is how a guard dies.

So the split is the one that works elsewhere in this package: FINDING is mechanical and complete (walk the
AST for cost calls carrying int literals — the parse tree either has them or it does not), and DECIDING is
agentic and recorded. The test reads the ledger and never calls a model, so CI stays offline; a site with
no verdict FAILS, so a new invented estimate cannot arrive by forgetting.

This is the sibling of token_caps.py — same discipline, different subject (that one guards the OUTPUT CAP
on a call, this one guards the PROVENANCE of a quoted price) — and it reuses that module's AST helpers
rather than growing a second copy of them.
"""
import ast
import json
import pathlib

from .token_caps import SKIP_DIRS, _enclosing, _iter_py

# The functions that turn token counts into money. A literal reaching one of these is a number nobody
# measured; whether that MATTERS is the question the model answers.
COST_FUNCS = ("realtime_cost", "batch_cost", "_cost", "estimate_jsonl_cost", "cost_of")

LEDGER_NAME = "estimate_literal_verdicts.json"

# The enum the prompt defines, so reading a verdict back later is reading a recorded answer.
VERDICT_QUOTE = "quoted_price"        # the result is presented as what something will cost -> must be measured
VERDICT_NOT_A_QUOTE = "not_a_quote"   # a priceability probe, a unit demo, a test fixture -> literals are fine
VERDICTS = (VERDICT_QUOTE, VERDICT_NOT_A_QUOTE)


def literal_sites(root):
    """Every cost-function call under `root` that passes a NON-ZERO int literal for a token count.

    Zero is excluded deliberately: `batch_cost(m, in_tok, 0)` for an embedding call states that there IS no
    output, which is a fact about the call, not a guess about its size."""
    found = []
    root = pathlib.Path(root)
    for p in _iter_py(root):
        try:
            src = p.read_text(errors="ignore")
            tree = ast.parse(src)
        except (SyntaxError, OSError):
            continue
        lines, encl = src.splitlines(), _enclosing(tree)
        for n in ast.walk(tree):
            if not isinstance(n, ast.Call):
                continue
            fn = n.func.attr if isinstance(n.func, ast.Attribute) else getattr(n.func, "id", "")
            if fn not in COST_FUNCS:
                continue
            lits = [a.value for a in n.args[1:]
                    if isinstance(a, ast.Constant) and isinstance(a.value, int)
                    and not isinstance(a.value, bool) and a.value]
            if not lits:
                continue
            found.append({
                "file": str(p.relative_to(root)), "symbol": encl.get(n.lineno, "<module>"),
                "fn": fn, "literals": lits, "line": n.lineno,
                "code": "\n".join(lines[max(0, n.lineno - 4):n.lineno + 3]).strip(),
            })
    return found


def site_key(site):
    """Ledger identity: where it is, which cost function, and WHICH NUMBERS. The literals are part of the
    key because changing them is a new decision — a verdict that survived an edit to the number would
    certify a quote nobody looked at."""
    return f"{site['file']}::{site['symbol']}::{site['fn']}{tuple(site['literals'])}"


ADJUDICATE_SYS = (
    "You are auditing a cost-accounting tool. You decide ONE thing about a call to a pricing function that "
    "was given literal token counts: is its RESULT PRESENTED AS A PRICE — what something will cost, shown "
    "to a person or used to authorize spending — or is it something else?\n\n"
    "This matters because a quoted price built from invented token counts is the defect that quoted $34 for "
    "a run that cost ~$380: the literals could not see that a reasoning model bills its hidden thinking as "
    "output. But the SAME function with the SAME shape is used for things that are not quotes at all — "
    "asking whether a model exists in the price table (`bool(realtime_cost(m, 1000, 1000))`, where the "
    "numbers are arbitrary), printing a per-1,000,000-token worked example (where 1000000 is the unit), or "
    "a test fixture. Those are correct as written and must not be flagged.\n\n"
    "Judge what the RESULT is used for in the surrounding code, not the size or roundness of the numbers.")

ADJUDICATE_SCHEMA = ('{"verdict": "quoted_price" or "not_a_quote", "why": "<one sentence naming what the '
                     'result is used for>"}')


def adjudicate_literal(site, model=None):
    """Is this call a quoted price, or something else? Agentic — the answer is what the result is used for."""
    from . import adapters, calls, config, output_contract
    model = model or config.advisor_model()
    with calls.context(intent="spendguard:estimate-literal"):
        r = adapters.call(
            model,
            f"file:   {site['file']}\nsymbol: {site['symbol']}\ncall:   {site['fn']} with literal token "
            f"counts {site['literals']}\n\ncode:\n{site['code']}\n\n"
            f"Is the RESULT of this call presented as a price?\nReply JSON only: {ADJUDICATE_SCHEMA}",
            sig="probe:estimate-literal", system=ADJUDICATE_SYS, reasoning="minimal")
    if r.get("error") or not r.get("text"):
        raise RuntimeError(f"no verdict for {site_key(site)}: {r.get('error') or 'no text'}")
    obj, _ = output_contract._as_obj(r["text"])
    if obj.get("verdict") not in VERDICTS:
        raise RuntimeError(f"verdict {obj.get('verdict')!r} is not one of {VERDICTS}")
    return {"verdict": obj["verdict"], "why": obj.get("why", ""), "model": model,
            "file": site["file"], "symbol": site["symbol"], "literals": site["literals"]}


def unruled_and_quotes(repo_root, scan_dirs=("src", "scripts")):
    """Literal-fed cost calls that exist vs verdicts on record. Both `unjudged` and `failed` fail the test."""
    from . import verdict_ledger
    repo_root = pathlib.Path(repo_root)
    present = {}
    for d in scan_dirs:
        if (repo_root / d).exists():
            for s in literal_sites(repo_root / d):
                s["file"] = f"{d}/{s['file']}"
                present[site_key(s)] = s
    return verdict_ledger.compare_to_verdicts(present, verdict_ledger.load_verdicts(repo_root, LEDGER_NAME),
                                  failing=(VERDICT_QUOTE,))


def cmd(argv=None):
    """`spendguard estimate-literals [--judge]` — list every literal-fed cost call; rule on the unjudged."""
    import sys
    argv = list(sys.argv[2:] if argv is None else argv)
    root = pathlib.Path(__file__).resolve().parents[2]
    res = unruled_and_quotes(root)
    print(f"{res['total']} cost call(s) with literal token counts · {len(res['cleared'])} ruled NOT a quote "
          f"· {len(res['failed'])} ruled QUOTED PRICE · {len(res['unjudged'])} UNJUDGED")
    for q in res["failed"]:
        print(f"  QUOTED PRICE  {q['file']}:{q['symbol']} {q['fn']}{tuple(q['literals'])} — {q.get('why','')}")
        print(f"                a quote must come from a measured sample (estimate.fit_from_sample)")
    for s in res["unjudged"]:
        print(f"  unjudged      {s['file']}:{s['symbol']} {s['fn']}{tuple(s['literals'])}")
    if "--judge" not in argv:
        return 1 if (res["unjudged"] or res["failed"]) else 0

    from . import verdict_ledger
    led = verdict_ledger.load_verdicts(root, LEDGER_NAME)
    for s in res["unjudged"]:
        v = adjudicate_literal(s)
        led[site_key(s)] = v
        print(f"  ruled {site_key(s)} -> {v['verdict']}: {v['why']}")
    verdict_ledger.save_verdicts(root, LEDGER_NAME, led)
    print(f"\nwrote {len(led)} verdict(s) -> {verdict_ledger.verdict_path(root, LEDGER_NAME)}")
    after = unruled_and_quotes(root)
    return 1 if (after["unjudged"] or after["failed"]) else 0
