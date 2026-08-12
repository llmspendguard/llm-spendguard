"""Which swallowed exceptions actually matter — decided by reading the code, not by counting them.

233 sites in this package answer a failure with `pass` or an empty return. That number alone is useless:
most are correct (a best-effort cache write, an optional import, a cosmetic terminal width probe), and a
handful are the reason a bug is invisible. `deid` swallowing its redaction failure left the RAW TEXT in
`out` and shipped it to a cloud LLM; `_save_state` swallowing a write error made a save that never happened
look exactly like one that did.

WHY THIS IS AGENTIC. "Does this failure matter?" depends on what the surrounding code was trying to
accomplish and what a caller will believe afterwards — which is meaning, not shape. A rule like "flag it if
the module is named budget/deid" is the mechanical proxy this codebase forbids: the deid bug was not in a
line mentioning privacy, and most lines in budget.py that swallow are genuinely fine. Each site goes to a
model with its enclosing function, and the question is the one that matters:

    AFTER THIS SWALLOWS, WHAT DOES A CALLER NOW BELIEVE THAT IS NOT TRUE?

Sites are FOUND mechanically — `except: pass` is a syntax fact the AST settles exactly. Only the judgement
is modelled.

CLI: triage_silent_failures.py [--run].  Estimate-first; caged.
"""
import argparse, ast, collections, json, os, pathlib, re, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import repo_defs                                    # noqa: E402

import spendguard                                   # noqa: F401,E402
spendguard.require()
from spendguard import adapters, calls, config, pricing, ui        # noqa: E402

SYSTEM = (
    "You are shown a function that swallows an exception. Decide what a CALLER wrongly believes afterwards.\n"
    "  benign      — the failure has no consequence a caller could act on: a best-effort cache, an optional\n"
    "                import, a cosmetic probe, cleanup of a temp file. Most sites are this. Say so.\n"
    "  misleading  — the caller now believes something FALSE: work was saved when it was not, a total is\n"
    "                complete when rows are missing, a value is zero when it is unknown, a check passed when\n"
    "                it never ran. This is the category that matters.\n"
    "  dangerous   — the swallow lets MONEY be mis-stated, PRIVATE TEXT escape, or ATTRIBUTION go to the\n"
    "                wrong party.\n"
    "Judge from what the code does, not from the module's name. A privacy bug rarely sits on a line that\n"
    "mentions privacy, and most swallows inside a money module are fine.")

SCHEMA = ('{"verdict": "benign|misleading|dangerous", "false_belief": "what a caller wrongly concludes", '
          '"fix": "what it should do instead — warn once, return None, re-raise, record UNKNOWN"}')


def sites(root):
    """Every swallowed exception, with its enclosing function. `except: pass` is settled by the AST."""
    out = []
    for f in sorted(pathlib.Path(root).glob("*.py")):
        txt = f.read_text()
        try:
            tree = ast.parse(txt)
        except SyntaxError:
            continue
        lines = txt.splitlines()
        parents = {}
        for fn in ast.walk(tree):
            if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for ch in ast.walk(fn):
                    parents.setdefault(id(ch), fn)
        for n in ast.walk(tree):
            if not isinstance(n, ast.ExceptHandler):
                continue
            body = n.body
            silent = (len(body) == 1 and isinstance(body[0], ast.Pass)) or (
                len(body) == 1 and isinstance(body[0], ast.Return)
                and (body[0].value is None or (isinstance(body[0].value, ast.Constant)
                                               and body[0].value.value in (None, 0, "", False, [], {}))))
            if not silent:
                continue
            fn = parents.get(id(n))
            if fn is None:
                continue
            out.append({"where": f"{f.stem}.{fn.name}", "line": n.lineno,
                        "src": "\n".join(lines[fn.lineno - 1:(fn.end_lineno or fn.lineno)])[:2600]})
    return out


REFUTE_SYS = (
    "You are shown a function that swallows an exception, and a CLAIM that the swallow is dangerous — that "
    "it lets money be mis-stated, private text escape, or attribution go to the wrong party. Your job is to "
    "REFUTE the claim. Look for the reasons it does not hold: the caller checks the return, the value is "
    "cosmetic, a higher layer already handles it, the failure cannot occur, or the 'dangerous' reading "
    "requires a caller that does not exist. Default to refuted=true when the case is not clearly made — a "
    "claim that survives should be one you could not knock down, not one you merely could not confirm.")

REFUTE_SCHEMA = '{"refuted": true|false, "why": "..."}'


def verify(rows, root, model, votes=2):
    """Keep only the dangerous findings that survive independent attempts to REFUTE them."""
    src = {d.qual: d.src for d in repo_defs.defs(root)}
    dangerous = [r for r in rows if (r.get("verdict") or {}).get("verdict") == "dangerous"]
    print(f"\n  verifying {len(dangerous)} dangerous finding(s), {votes} refuters each")
    kept = []
    for i, r in enumerate(dangerous, 1):
        body = src.get(r["where"], "")
        claim = (r["verdict"] or {}).get("false_belief", "")
        vs = []
        for k in range(votes):
            with calls.context(intent="spendguard:silent-failure-verify"):
                resp = adapters.call_complete(model,
                    f"```python\n{body[:2400]}\n```\n\nCLAIM about the swallow at line(s) {r['lines']}: "
                    f"a caller wrongly believes — {claim}\n\nRefute it.\nReply JSON only: {REFUTE_SCHEMA}",
                    sig="probe:refute", system=REFUTE_SYS)
            # 300 TOKENS TRUNCATED THE REFUTERS' REPLIES into unparseable JSON, and this loop then reported
            # "no refuter answered" — 19 findings came back UNVERIFIED for a reason that had nothing to do
            # with the findings. A truncated answer is not an absent one; it is a budget bug wearing the
            # costume of a silent verifier, and it is the same `truncated != clean` mistake this codebase
            # keeps finding elsewhere.
            if resp.get("error"):
                continue
            txt = resp.get("text") or ""
            try:
                blob = re.search(r"\{.*\}", txt, re.S)
                vs.append(json.loads(blob.group(0)) if blob else None)
            except Exception:
                print(f"      (a refuter reply did not parse — {len(txt)} chars, "
                      f"{'looks TRUNCATED' if txt and not txt.rstrip().endswith('}') else 'malformed'})")
        vs = [v for v in vs if v]
        if not vs:
            print(f"  {i}/{len(dangerous)} {r['where']}: UNVERIFIED (no refuter answered) — kept, not confirmed")
            r["survived"] = None
            kept.append(r)
            continue
        # NO MAJORITY VOTE. `sum(not refuted) > len(vs)/2` decided "did this claim survive?" with a
        # hand-picked cutoff, and it is the wrong instrument twice over: it treats a confident refutation and
        # a hedged one as one vote each, and on a 1-1 split it resolves the disagreement by arithmetic rather
        # than by reading what the refuters actually said. Unanimity is NOT a threshold — it is the absence of
        # disagreement, and it stands on its own. A SPLIT is a judgement, so it goes to an adjudicator that
        # reads the claim and every refutation and decides which side is right.
        refuted = [bool(v.get("refuted")) for v in vs]
        if all(refuted):
            survives = False
        elif not any(refuted):
            survives = True
        else:
            args = "\n".join(f"- refuter {j + 1} says {'REFUTED' if v.get('refuted') else 'STANDS'}: "
                             f"{str(v.get('why'))[:400]}" for j, v in enumerate(vs))
            with calls.context(intent="spendguard:silent-failure-adjudicate"):
                adj = adapters.call_complete(model,
                    f"```python\n{body[:2400]}\n```\n\nCLAIM: a caller wrongly believes — {claim}\n\n"
                    f"The refuters DISAGREED:\n{args}\n\nRead the code and decide which side is right.\n"
                    f'Reply JSON only: {{"claim_stands": true|false, "why": "..."}}',
                    sig="probe:adjudicate",
                    system="You settle a disagreement between reviewers by reading the code yourself. The "
                           "refuters split; their arguments are given. Decide whether the claim about the "
                           "swallowed exception is right. Say which specific refutation you found convincing "
                           "or wrong, and why — do not average them.")
            survives = True                      # unresolved disagreement is not a clean bill
            try:
                b2 = re.search(r"\{.*\}", adj.get("text") or "", re.S)
                if b2:
                    survives = bool(json.loads(b2.group(0)).get("claim_stands"))
            except Exception:
                pass
            print(f"      (refuters split {sum(refuted)}/{len(vs)} — adjudicated: "
                  f"{'claim STANDS' if survives else 'refuted'})")
        r["survived"] = survives
        if survives:
            kept.append(r)
            print(f"  ✓ SURVIVES  {r['where']}:{r['lines'][0]}  {claim[:88]}")
    # "KEPT" IS NOT "SURVIVED". This printed len(kept)/len(dangerous) as "survived refutation", and kept
    # includes the UNVERIFIED ones — findings whose refuters never answered. So a run where 10 were refuted,
    # 2 genuinely survived and 7 could not be checked reported "9/19 survived", and I repeated that number to
    # the user as though nine findings had been confirmed. The three states mean different things and only
    # one of them is a finding, so they are counted separately.
    #
    # NOTE FOR THE DECISION-HOOK: nothing below DECIDES anything. `r["survived"]` was written above by the
    # refuters and, on a split, by the adjudicator — models, in both cases. This only TALLIES verdicts that
    # a model already reached, and the three-way split is the schema those verdicts are recorded in
    # (True = survived, False = refuted, None = never answered). Counting recorded values is arithmetic.
    verdicts = [r.get("survived") for r in kept]          # already-decided values, straight from the model
    n_surv = verdicts.count(True)
    n_unv = verdicts.count(None)
    print(f"\n  {n_surv} SURVIVED refutation · {n_unv} UNVERIFIED (refuter never answered — not a finding "
          f"and not a clean bill) · {len(dangerous) - len(kept)} refuted")
    return kept


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default="src/spendguard")
    ap.add_argument("--model", default="")
    ap.add_argument("--run", action="store_true")
    ap.add_argument("--out", default="")
    ap.add_argument("--verify", action="store_true", help="refute each dangerous finding before believing it")
    ap.add_argument("--from-file", default="", help="re-verify an existing triage jsonl instead of re-judging")
    a = ap.parse_args()
    os.environ["SPENDGUARD_ADVISOR_EXECUTOR"] = "api"

    if a.from_file:
        rows = [json.loads(l) for l in open(a.from_file)]
        model = a.model or config.advisor_model()
        kept = verify(rows, a.root, model)
        out = a.out or os.path.join(str(config.HOME), "silent_failures_verified.jsonl")
        with open(out, "w") as fh:
            for r_ in kept:
                fh.write(json.dumps(r_) + "\n")
        print(f"  -> {out}")
        return 0

    ss = sites(a.root)
    # One call per enclosing FUNCTION, not per handler — several swallows in one body are one judgement.
    by_fn = collections.OrderedDict()
    for s in ss:
        cur = by_fn.setdefault(s["where"], {"where": s["where"], "src": s["src"], "lines": []})
        cur["lines"].append(s["line"])
    model = a.model or config.advisor_model()
    est = sum(pricing.realtime_cost(model, len(s["src"]) // 4 + 250, 350) or 0 for s in by_fn.values())
    print(f"{len(ss)} swallowed exceptions in {len(by_fn)} functions")
    if not a.run:
        ui.estimate_only(action=f"judge {len(by_fn)} swallowing functions: benign / misleading / dangerous",
                         cost=est)
        return 0

    rows, counts = [], collections.Counter()
    for i, (where, s) in enumerate(by_fn.items(), 1):
        prompt = (f"```python\n{s['src']}\n```\n\nThis function swallows an exception at line(s) "
                  f"{sorted(set(s['lines']))}. After it swallows, what does a caller wrongly believe?\n"
                  f"Reply JSON only: {SCHEMA}")
        with calls.context(intent="spendguard:silent-failure-triage"):
            r = adapters.call_complete(model, prompt, sig="probe:silent-triage", system=SYSTEM)
        v = None
        if not r.get("error"):
            try:
                blob = re.search(r"\{.*\}", r.get("text") or "", re.S)
                v = json.loads(blob.group(0)) if blob else None
            except Exception:
                v = None
        verdict = (v or {}).get("verdict") or "UNJUDGED"
        counts[verdict] += 1
        if verdict in ("dangerous", "misleading"):
            print(f"\n  {'⛔' if verdict == 'dangerous' else '⚠'} {verdict.upper()}  {where}:{sorted(set(s['lines']))[0]}")
            print(f"      believes: {str((v or {}).get('false_belief'))[:120]}")
            print(f"      fix: {str((v or {}).get('fix'))[:110]}")
        rows.append({"where": where, "lines": sorted(set(s["lines"])), "verdict": v})
        if i % 40 == 0:
            print(f"    …{i}/{len(by_fn)} judged", flush=True)
    out = a.out or os.path.join(str(config.HOME), "silent_failures.jsonl")
    with open(out, "w") as fh:
        for r_ in rows:
            fh.write(json.dumps(r_) + "\n")
    print(f"\n  {dict(counts)}")
    print(f"  UNJUDGED is not benign — those functions were never read.  -> {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
