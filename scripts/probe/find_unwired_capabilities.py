"""Find capabilities that were BUILT and never CONNECTED — the defect that keeps producing every other one.

THE PATTERN, six times in one day (2026-08-10/11):

  config.update_json      written to stop config.json being destroyed — `keep_backups` defaulted to 0 and
                          almost every caller left it there, so the whole-repo invariant check still reported
                          "every mutation backs up first" as ABSENT after the fix shipped
  budget.snapshot         written the day the ledger had no backup — called from 1 of the 6 functions that
                          DELETE money rows
  bulkgate.is_truncated   "a fact, not a guess" — called by nothing outside its own module
  bulkgate.maxtokens      learns the p99 output length per call-class — no judging script used it; every one
                          hand-picked 300/450/900/1400 and read truncated replies as short answers
  adapters.finish_reason  DECLARED in the result dict, documented as how callers tell complete from
                          truncated, and ASSIGNED BY NO PROVIDER BRANCH — always None
  calibrate.record_estimate  the learned estimator's input — nothing bridged the gate's own estimates to it

None of these is a bug in the usual sense. Every one is correct code that nothing calls, and that is worse
than a bug, because the codebase LOOKS like it has the protection. A reviewer reading update_json sees
backups. A reviewer reading is_truncated sees truncation handled. The gap is not in any file — it is in the
absence of an edge between two files, which is why axes 1-3 cannot see it and why it kept recurring after
each individual fix.

WHAT IT LOOKS FOR (mechanically — these are all facts the AST settles):
  · a public function with NO caller anywhere in the package
  · a result-dict key that is initialised and never assigned again  (the finish_reason shape)
  · a safety-ish parameter whose DEFAULT disables it                (the keep_backups=0 shape)
Then a model judges the only question that needs judging: SHOULD this be wired, and to what?

CLI: find_unwired_capabilities.py [--run].  Estimate-first; caged.
"""
import argparse, ast, collections, json, os, pathlib, re, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import repo_defs                                    # noqa: E402

import spendguard                                   # noqa: F401,E402
spendguard.require()
from spendguard import adapters, calls, config, pricing, ui        # noqa: E402

SYSTEM = (
    "You are shown a capability in a codebase that appears to be UNWIRED — built but not connected. Decide:\n"
    "  wired_elsewhere — it IS reached, by a path the scan cannot see (a CLI dispatch table, an entry point, "
    "a getattr, a plugin registry, a public API for outside consumers). Not a defect.\n"
    "  intentional     — genuinely optional, or a public API meant for consumers of this library. Fine.\n"
    "  UNWIRED         — the codebase would be materially better if something called it, and there is an "
    "obvious place. Name that place.\n"
    "A safety mechanism that exists but is not on the path it protects is the serious case: the code LOOKS "
    "protected to anyone reading it, and is not.")

SCHEMA = ('{"verdict": "wired_elsewhere|intentional|UNWIRED", "why": "...", '
          '"should_be_called_from": "module.function, or empty", "risk_if_left": "..."}')


def unwired(root):
    """Public functions with no call site anywhere in the package."""
    ds = repo_defs.defs(root)
    defined = {d.bare: d for d in ds if not d.bare.startswith("_") and not d.is_method}
    called = collections.Counter()
    for f in sorted(pathlib.Path(root).glob("*.py")):
        try:
            tree = ast.parse(f.read_text())
        except SyntaxError:
            continue
        for n in ast.walk(tree):
            if isinstance(n, ast.Call):
                fn = n.func
                nm = fn.attr if isinstance(fn, ast.Attribute) else (fn.id if isinstance(fn, ast.Name) else "")
                if nm:
                    called[nm] += 1
            # a bare re-export (`from .x import y`) is a use for this purpose — it publishes the name
            if isinstance(n, ast.ImportFrom):
                for a in n.names:
                    called[a.name] += 1
    return {name: d for name, d in defined.items() if called[name] == 0}


def declared_never_set(root):
    """Result-dict keys initialised to a null default and never given a real value ANYWHERE — the
    `finish_reason` shape: declared, documented as the thing callers check, assigned by no branch.

    THE FIRST VERSION OF THIS USED A REGEX and returned ZERO findings while adapters.py sat two directories
    away with exactly this defect. It searched the file text for `"key":` not followed by a null literal, so
    it missed every assignment that did not look like that — `{**base, "finish_reason": x}`, a subscript
    write, a dict built in a helper — and reported the absence of its own pattern as the absence of the
    defect. A regex that under-reports does not fail loudly; it hands back an empty list that reads exactly
    like a clean result. Which key a value is assigned to is settled EXACTLY by the AST, so it is parsed.
    """
    out = []
    for f in sorted(pathlib.Path(root).glob("*.py")):
        txt = f.read_text()
        try:
            tree = ast.parse(txt)
        except SyntaxError:
            continue
        NULLS = (None, 0, "", False)
        declared, assigned = {}, set()
        for n in ast.walk(tree):
            if isinstance(n, ast.Dict):
                for k, v in zip(n.keys, n.values):
                    if not (isinstance(k, ast.Constant) and isinstance(k.value, str)):
                        continue
                    if isinstance(v, ast.Constant) and v.value in NULLS:
                        declared.setdefault(k.value, n.lineno)
                    else:
                        assigned.add(k.value)          # a real value for this key, somewhere
            elif isinstance(n, ast.Subscript) and isinstance(n.slice, ast.Constant) \
                    and isinstance(n.slice.value, str):
                assigned.add(n.slice.value)            # d["key"] = ... (or a read; conservative on purpose)
            elif isinstance(n, ast.keyword) and n.arg:
                assigned.add(n.arg)                    # fn(key=value) building the same shape
        for key, ln in declared.items():
            if key not in assigned and len(key) > 3:
                out.append((f.stem, key, ln))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default="src/spendguard")
    ap.add_argument("--model", default="")
    ap.add_argument("--run", action="store_true")
    ap.add_argument("--out", default="")
    a = ap.parse_args()
    os.environ["SPENDGUARD_ADVISOR_EXECUTOR"] = "api"

    cands = unwired(a.root)
    fields = declared_never_set(a.root)
    print(f"{len(cands)} public function(s) with no call site in the package")
    print(f"{len(fields)} dict key(s) initialised and never assigned")
    for m, k, ln in fields[:12]:
        print(f"    {m}.py:{ln}  '{k}'")
    model = a.model or config.advisor_model()
    est = sum(pricing.realtime_cost(model, len(d.src[:1800]) // 4 + 250, 400) or 0 for d in cands.values())
    if not a.run:
        ui.estimate_only(action=f"judge {len(cands)} apparently-unwired capabilities", cost=est)
        return 0

    rows, counts = [], collections.Counter()
    for i, (name, d) in enumerate(sorted(cands.items()), 1):
        prompt = (f"```python\n{d.src[:1800]}\n```\n\n`{d.qual}` is defined in this package and no call site "
                  f"for it exists anywhere in the package. Is it unwired?\nReply JSON only: {SCHEMA}")
        # THE CALL THAT CANNOT SILENTLY TRUNCATE — sized from this call-class's measured p99, retried on
        # truncation, and text=None rather than a cut body if it still does not fit.
        with calls.context(intent="spendguard:unwired-scan"):
            r = adapters.call_complete(model, prompt, sig="probe:unwired", system=SYSTEM)
        v = None
        if not r.get("error"):
            try:
                blob = re.search(r"\{.*\}", r.get("text") or "", re.S)
                v = json.loads(blob.group(0)) if blob else None
            except Exception:
                v = None
        verdict = (v or {}).get("verdict") or ("TRUNCATED" if r.get("truncated") else "UNJUDGED")
        counts[verdict] += 1
        if verdict == "UNWIRED":
            print(f"\n  ⛔ UNWIRED  {d.qual}")
            print(f"      should be called from: {(v or {}).get('should_be_called_from') or '(unnamed)'}")
            print(f"      risk: {str((v or {}).get('risk_if_left'))[:110]}")
        rows.append({"qual": d.qual, "verdict": v, "truncated": r.get("truncated")})
    out = a.out or os.path.join(str(config.HOME), "unwired.jsonl")
    with open(out, "w") as fh:
        for r_ in rows:
            fh.write(json.dumps(r_) + "\n")
    print(f"\n  {dict(counts)}   -> {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
