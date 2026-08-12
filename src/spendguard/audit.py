"""Guard: find scripts that hardcode an OpenAI price disagreeing with pricing.py.

Catches the class of bug that caused the cost surprise: a gpt-5.5 rate literal
that isn't the canonical (5.00/30.00 realtime, 2.50/15.00 batch). Run it before
trusting any script's "$ estimate", and after editing prices.

  spendguard audit             # report
  spendguard audit --ci        # exit 1 if any gpt-5.5 mispricing found
  python -m spendguard.audit   # same, without the CLI

RUN IT AS A MODULE, NOT AS A FILE. This module is part of the package and imports `.pricing`
relatively, so `python src/spendguard/audit.py` raises ImportError before reaching a single line of
its own code. The docstring used to name a `scripts/…` path from a layout that no longer exists —
so the one instruction here was the one thing guaranteed to fail. `-m` runs the same __main__ block
with the package context the imports need.
"""
import os, re, sys, glob, json

from .pricing import PRICING

SCRIPTS = os.getenv("SPENDGUARD_AUDIT_DIR") or os.getcwd()  # dir of code to scan for stray price literals
# Directory names excluded from the scan, overridable so the policy is not baked into the tool. Test trees
# hold banned literals deliberately; build and vendor trees are not this repo's code. Every exclusion is
# COUNTED AND NAMED in the report — see the skip loop below for why silence is the real hazard.
EXCLUDE_DIRS = set(filter(None, (os.getenv("SPENDGUARD_AUDIT_EXCLUDE")
                                 or "tests,test,.venv,.venv.nosync,node_modules,build,dist,site-packages"
                                 ).split(",")))
# Match the price pair attached SPECIFICALLY to a gpt-5.5 / gpt-5.5-pro dict key,
# e.g.  "gpt-5.5": (1.25, 10.0)  — not other models that share the line.
KEYED = re.compile(r"""["'](gpt-?5\.5(?:-pro)?)["']\s*:\s*\(\s*([0-9.]+)\s*,\s*([0-9.]+)\s*\)""", re.I)
ALLOWED = {
    "gpt-5.5": {(PRICING["gpt-5.5"]["in_"], PRICING["gpt-5.5"]["out"]),
                (PRICING["gpt-5.5"]["batch_in"], PRICING["gpt-5.5"]["batch_out"])},
    "gpt-5.5-pro": {(PRICING["gpt-5.5-pro"]["in_"], PRICING["gpt-5.5-pro"]["out"]),
                    (PRICING["gpt-5.5-pro"]["batch_in"], PRICING["gpt-5.5-pro"]["batch_out"])},
}

# Specific known-wrong literals that have burned us — banned in any form, any dict shape.
# THE DECIMAL POINT WAS REQUIRED, SO INTEGERS SLIPPED THROUGH. `15\.0+` matches 15.0 and 15.00 and never
# plain `15`, so `(15, 75)` — the most natural way to write the very rate this guard exists to ban — passed
# the audit clean. A guard that only catches one spelling of the thing it bans reports "no violations" on a
# file containing the violation, which is worse than no guard: it is a certificate.
#
# (These regexes are PARSING, not judging: a price tuple has a fixed literal shape and the question is
# whether that exact shape appears, not what any text means.)
BANNED = [
    (re.compile(r"\(\s*15(?:\.0+)?\s*,\s*75(?:\.0+)?\s*\)"), "old-Opus rate (15/75) — opus-4.8 is (5.0, 25.0)"),
    (re.compile(r"\(\s*15(?:\.0+)?\s*,\s*120(?:\.0+)?\s*\)"), "wrong gpt-5.5-pro (15/120) — should be (30,180) rt / (15,90) batch"),
    (re.compile(r"[\"']gpt-?5\.5[\"']\s*:\s*\{[^}]*[\"']out[\"']\s*:\s*40"), "gpt-5.5 out=40 — should be 30"),
    (re.compile(r"[\"']gpt-?5\.5[\"']\s*:\s*\(\s*1\.25\s*,\s*10"), "gpt-5.5 priced as old gpt-5 (1.25/10) — should be (5,30) rt"),
]

DEEP_SYS = (
    "You are given a source file and the project's CANONICAL price table. Answer one question: does this "
    "file hardcode an LLM price that disagrees with that table? Look for rates in ANY form — a literal "
    "tuple, a dict like {'in': 15, 'out': 75}, two separate constants, a number multiplied into a cost "
    "calculation, a comment stating a rate the code then relies on. A pattern scan already checked literal "
    "tuples next to a known model key and found nothing, so those are not what you are for. Reading a price "
    "FROM the pricing module is correct and is not a finding. When you are unsure whether a number is a "
    "price at all, say so rather than guessing — a false alarm here sends someone to 'fix' a constant that "
    "was never a rate.")

DEEP_SCHEMA = ('{"hardcoded_prices": [{"line": 0, "snippet": "...", "model": "...", '
               '"why_wrong": "how it disagrees with the canonical table", '
               '"confidence": "certain|likely|unsure"}]}')


def deep(paths, canonical, run=False):
    """The question the pattern scan cannot ask: does this file hardcode a price that disagrees?

    The BANNED/ALLOWED patterns match literal tuples next to a known model key. That is one spelling of one
    shape, and the audit's old "OK" line reported the absence of THAT as the absence of a wrong price.
    Whether an arbitrary number in arbitrary code is a rate, and whether it contradicts the table, is a
    judgement about what the code MEANS — so it goes to a model, with the pattern scan kept as the fast
    pass that runs first and never has the last word.
    """
    from . import adapters, calls, pricing as _p, ui
    model = _cfg_advisor()
    est = sum(_p.realtime_cost(model, len(open(p, errors="ignore").read()) // 4 + 400, 500) or 0
              for p in paths)
    if not run:
        ui.estimate_only(action=f"read {len(paths)} file(s) for hardcoded prices in ANY form", cost=est)
        return []
    found = []
    for i, p in enumerate(paths, 1):
        src = open(p, errors="ignore").read()
        with calls.context(intent="spendguard:audit-deep"):
            r = adapters.call(model, f"CANONICAL PRICES:\n{canonical}\n\nFILE {os.path.basename(p)}:\n"
                                     f"```python\n{src[:12000]}\n```\n\nReply JSON only: {DEEP_SCHEMA}",
                              sig="probe:audit-deep", system=DEEP_SYS)
        if r.get("error"):
            print(f"  {os.path.basename(p)}: UNREAD ({str(r['error'])[:60]}) — not the same as clean")
            continue
        try:
            blob = re.search(r"\{.*\}", r.get("text") or "", re.S)
            for h in (json.loads(blob.group(0)).get("hardcoded_prices") if blob else []) or []:
                found.append((os.path.basename(p), h))
        except Exception:
            print(f"  {os.path.basename(p)}: reply unparseable — UNREAD, not clean")
        if i % 20 == 0:
            print(f"    …{i}/{len(paths)} read", flush=True)
    return found


def _cfg_advisor():
    from . import config
    return config.advisor_model()


def main(argv=None):
    argv = sys.argv[1:] if argv is None else list(argv)
    ci = "--ci" in argv
    is_deep = "--deep" in argv
    hits = []
    scanned, skipped = [], []   # DENOMINATOR and EXCLUSIONS: "no hits in 0 files" is not a pass,
                                # and a tree skipped in silence is a clean bill for unread code
    # RECURSIVE. This globbed `<dir>/*.py` — top level only — so running it from a repo root scanned the
    # handful of files that happen to sit there and reported clean on a tree whose actual code is all one
    # directory down. A price audit that silently skips src/ is not a weaker audit; it is a certificate for
    # the files it never opened. The denominator printed at the end makes the scope visible either way.
    for path in sorted(glob.glob(os.path.join(SCRIPTS, "**", "*.py"), recursive=True)):
        # skip files that legitimately CONTAIN the wrong literals to describe/test them (the audit itself,
        # the canonical table, the reconcilers) — else the audit flags its own examples.
        if os.path.basename(path) in ("pricing.py", "audit.py", "audit_price_constants.py",
                                      "reconcile_openai_spend.py", "reconcile_openai.py"):
            skipped.append(path)
            continue
        # TEST FIXTURES CONTAIN BANNED LITERALS ON PURPOSE. Making the scan recursive immediately surfaced
        # 14 "findings", every one inside tests/test_audit.py — the file that feeds this detector the exact
        # wrong prices it exists to catch. A guard that documents a pattern will always contain the pattern.
        #
        # THIS IS SCOPE, NOT A VERDICT, and the two need different instruments. Which directories to scan is
        # a configuration choice with a near-universal convention behind it; asking a model "is this file a
        # test?" for every file in every repo would be slow and would still be a guess. What actually
        # matters is that the exclusion CANNOT BE SILENT — a scan that quietly skips a tree reports clean on
        # code it never opened, which is the failure this whole module keeps producing. So: the directories
        # are configurable (no hardcoded policy), and every skipped file is counted and named in the report.
        parts = os.path.normpath(path).split(os.sep)
        if EXCLUDE_DIRS & set(parts):
            skipped.append(path)
            continue
        with open(path, errors="ignore") as fh:      # closed deterministically: this walks a whole tree
            lines = list(enumerate(fh, 1))
        scanned.append(path)                         # counted AFTER the skips, so the denominator is real
        for i, line in lines:
            for m in KEYED.finditer(line):
                key = m.group(1).lower()
                pair = (float(m.group(2)), float(m.group(3)))
                if pair not in ALLOWED.get(key, set()):
                    hits.append((os.path.basename(path), i, f"{key}={pair}", line.strip()[:90]))
            for rx, msg in BANNED:
                if rx.search(line):
                    hits.append((os.path.basename(path), i, msg, line.strip()[:90]))

    if not hits:
        # WHAT WAS CHECKED, NOT "OK". This printed "OK: no gpt-5.5 price literal disagrees with canonical
        # pricing.py" — a clean bill issued by a fixed list of literal SPELLINGS. It cannot see
        # `{"in": 15, "out": 75}`, `IN_RATE = 15` split across two constants, a rate arrived at by
        # arithmetic, or any model not in BANNED/ALLOWED. Every one of those is a hardcoded wrong price
        # that this audit passes, and the word "OK" is what turns a narrow check into a false assurance —
        # the exact shape that let 15/120 sit in a script while an audit reported nothing wrong.
        print(f"no BANNED literal or mismatched keyed pair found in {len(scanned)} file(s) "
              f"(recursive from {SCRIPTS}).")
        if skipped:
            print(f"  SKIPPED {len(skipped)} file(s) in excluded dirs ({', '.join(sorted(EXCLUDE_DIRS))}) "
                  f"— override with SPENDGUARD_AUDIT_EXCLUDE. Not scanned is not clean.")
        print("  CHECKED: literal price pairs written as a tuple next to a known model key.")
        print("  NOT CHECKED: dict-form rates ({'in': 15, 'out': 75}), rates split across constants,")
        print("               computed rates, and any model absent from the canonical table.")
        print("  This is 'the patterns did not fire', not 'no wrong price is hardcoded here'.")
        if not is_deep:
            print("  For the real question, ask a reader that understands the code: "
                  "`spendguard audit --deep` (caged, agentic; add --run to spend).")
            return 0
        # --deep asks the question the patterns cannot. It runs on the SAME files the scan cleared, because
        # those are precisely the ones a clean pattern result would otherwise close the book on.
        print(f"\n--deep: reading {len(scanned)} file(s) for prices in ANY form …")
        deep_hits = deep(scanned, repr(PRICING)[:4000], run="--run" in argv)
        if not deep_hits:
            print("  no hardcoded price found by reading the code either." if "--run" in argv else "")
            return 0
        print(f"\nFOUND {len(deep_hits)} hardcoded price(s) the pattern scan could not see:\n")
        for fn, h in deep_hits:
            print(f"  {fn}:{h.get('line')}  [{h.get('confidence')}]  {str(h.get('model'))}")
            print(f"      {str(h.get('snippet'))[:100]}")
            print(f"      {str(h.get('why_wrong'))[:110]}")
        return 1 if ci else 0
    print(f"FOUND {len(hits)} gpt-5.5 price literal(s) not matching canonical pricing.py "
          f"(gpt-5.5 realtime 5.0/30.0 or batch 2.5/15.0; pro 30.0/180.0 or 15.0/90.0):\n")
    for fn, ln, pair, txt in hits:
        print(f"  {fn}:{ln}  {pair}   {txt}")
    print("\nFix: replace with `from pricing import batch_cost` or use the canonical pair.")
    return 1 if ci else 0

if __name__ == "__main__":
    sys.exit(main())
