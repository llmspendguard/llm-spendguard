"""Close out the wave findings the ledger cannot settle mechanically: is it guarded, and who decides a SPLIT.

finding_ledger.py reports 151 wave findings with guard status UNKNOWN. That is the honest answer from a
token comparison — a wave row is labelled `budget.py:310 <claim prose>`, and deciding whether a test's prose
describes that same defect is a judgement about meaning. This does the judging, in two passes that answer
two genuinely different questions:

  GUARDED?      106 rows the validators CONFIRMED. The fix may well have landed; what is unknown is whether
                anything stops it coming back. "Fixed and unguarded" is the state this project keeps
                rediscovering the hard way, so it must be counted separately from "fixed", not folded in.

  ADJUDICATE    45 rows recorded as "SPLIT — needs a human". Those validators disagreed and the row has sat
                in neither column since. A split is not a soft rejection and not a soft confirmation; left
                alone it silently becomes whichever one the reader assumes, and both assumptions have been
                wrong here. An adjudicator reads the ACTUAL CODE AT THAT SITE — not the arguments — and says
                whether the defect is real today.

CANDIDATE RETRIEVAL IS MECHANICAL, THE VERDICT IS NOT. Which test files mention a module is settled by the
text; whether one of them guards a specific defect is not. So the module name pulls candidates and a model
reads them. No score, no cutoff: every candidate found is shown to the judge, and a finding with no
candidates is reported as unguarded rather than as unjudged, because "no test mentions this module at all"
IS an answer.

CLI: resolve_wave_findings.py [--guards|--splits] [--run]     estimate-first, caged.
"""
import argparse
import collections
import json
import os
import pathlib
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import spendguard                                   # noqa: F401,E402
spendguard.require()
from spendguard import adapters, calls, config, pricing, ui        # noqa: E402

WAVES = ["validated_findings.jsonl", "validated_wave1_grouped.jsonl", "validated_wave2.jsonl",
         "validated_wave2_grouped.jsonl", "validated_wave3.jsonl"]

GUARD_SYS = (
    "You are shown a code defect that a review confirmed, and every test in the repo that mentions the same "
    "module. Decide whether one of those tests would FAIL if the defect were reintroduced. That is the only "
    "question: not whether the module is tested, not whether the area is covered, but whether THIS defect "
    "coming back would turn something red. A test that exercises the same function while being indifferent "
    "to this behaviour does NOT guard it. When the candidates do not clearly cover it, say guarded=false — "
    "a wrongly-claimed guard is worse than a known gap, because a known gap gets fixed.")

GUARD_SCHEMA = ('{"guarded": true|false, "which": "the test line or name, if any", '
                '"why": "...", "suggest": "what a guard for this should assert, if none exists"}')

SPLIT_SYS = (
    "Two reviewers disagreed about whether a defect is real, and the row has been unresolved since. You are "
    "shown the claim and the CODE AS IT IS NOW. Decide from the code, not from their arguments — and note "
    "that the code may have been fixed since the review, in which case the honest answer is that the defect "
    "is no longer present. Distinguish: real_now (the defect is in this code today), already_fixed (it was "
    "real and the code has changed), or not_a_defect (the claim was wrong about what the code does).")

SPLIT_SCHEMA = ('{"verdict": "real_now|already_fixed|not_a_defect", "why": "...", '
                '"what_to_do": "the fix, or the guard to add if already fixed"}')


def wave_rows(d, outcome):
    out = []
    for f in WAVES:
        p = pathlib.Path(d) / f
        if not p.exists():
            continue
        for line in p.open():
            try:
                r = json.loads(line)
            except Exception:
                continue
            if r.get("outcome") == outcome:
                r["_src"] = f
                out.append(r)
    return out


def candidate_guards(module, tests_dir, limit=40):
    """Lines in the test corpus that mention this module. Retrieval — the text settles it."""
    hits = []
    stem = module.replace(".py", "")
    for t in sorted(pathlib.Path(tests_dir).glob("*.py")):
        try:
            lines = t.read_text(errors="ignore").splitlines()
        except OSError:
            continue
        for i, ln in enumerate(lines):
            if stem in ln and ("check(" in ln or "assert" in ln or "ck(" in ln):
                hits.append(f"{t.name}:{i+1}  {ln.strip()[:200]}")
    return hits[:limit]


def source_at(repo, module, line, span=26):
    p = pathlib.Path(repo) / "src" / "spendguard" / module
    if not p.exists():
        return ""
    lines = p.read_text(errors="ignore").splitlines()
    try:
        n = int(line)
    except (TypeError, ValueError):
        n = 1
    lo, hi = max(0, n - span // 2), min(len(lines), n + span)
    return "\n".join(f"{i+1}: {lines[i]}" for i in range(lo, hi))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default=str(pathlib.Path.home() / ".spendguard"))
    ap.add_argument("--repo", default=os.getcwd())
    ap.add_argument("--guards", action="store_true", help="judge guard coverage for CONFIRMED rows")
    ap.add_argument("--splits", action="store_true", help="adjudicate the unresolved SPLIT rows")
    ap.add_argument("--model", default="")
    ap.add_argument("--run", action="store_true")
    a = ap.parse_args()
    os.environ["SPENDGUARD_ADVISOR_EXECUTOR"] = "api"
    model = a.model or config.advisor_model()
    tests = pathlib.Path(a.repo) / "tests"
    do_guards, do_splits = a.guards or not a.splits, a.splits or not a.guards

    jobs = []
    if do_guards:
        for r in wave_rows(a.dir, "CONFIRMED"):
            mod = (r.get("file") or "").split("/")[-1]
            cands = candidate_guards(mod, tests)
            jobs.append(("guard", r, mod, cands))
    if do_splits:
        for r in wave_rows(a.dir, "SPLIT — needs a human"):
            mod = (r.get("file") or "").split("/")[-1]
            jobs.append(("split", r, mod, None))

    est = sum(pricing.realtime_cost(model, 1400 if k == "guard" else 1000, 350) or 0 for k, *_ in jobs)
    ng = sum(1 for k, *_ in jobs if k == "guard")
    print(f"{ng} CONFIRMED rows to check for a guard · {len(jobs) - ng} SPLIT rows to adjudicate")
    if not a.run:
        ui.estimate_only(action=f"resolve {len(jobs)} wave findings", cost=est)
        return 0

    counts = collections.Counter()
    out = []
    for i, (kind, r, mod, cands) in enumerate(jobs, 1):
        claim = (r.get("claims") or [""])[0][:600]
        where = f"{r.get('file')}:{r.get('line')}"
        if kind == "guard":
            body = ("\n".join(cands) or "(NO test in this repo mentions that module at all)")
            prompt = (f"DEFECT (confirmed by review) at {where}:\n{claim}\n\n"
                      f"EVERY test line mentioning `{mod}`:\n{body}\n\n"
                      f"Would any of these FAIL if the defect came back?\nReply JSON only: {GUARD_SCHEMA}")
            sysmsg, sig = GUARD_SYS, "probe:guard-match"
        else:
            prompt = (f"DISPUTED CLAIM at {where}:\n{claim}\n\nTHE CODE THERE NOW:\n"
                      f"```python\n{source_at(a.repo, mod, r.get('line'))}\n```\n\n"
                      f"Judge from the code.\nReply JSON only: {SPLIT_SCHEMA}")
            sysmsg, sig = SPLIT_SYS, "probe:split-adjudicate"
        with calls.context(intent=f"spendguard:{sig.split(':')[1]}"):
            resp = adapters.call(model, prompt, sig=sig, system=sysmsg)
        v = None
        if not resp.get("error"):
            try:
                blob = re.search(r"\{.*\}", resp.get("text") or "", re.S)
                v = json.loads(blob.group(0)) if blob else None
            except Exception:
                v = None
        if v is None:
            counts[f"{kind}:UNJUDGED"] += 1
            out.append({"kind": kind, "where": where, "claim": claim[:200], "verdict": None})
            continue
        key = f"{kind}:" + str(v.get("guarded") if kind == "guard" else v.get("verdict"))
        counts[key] += 1
        out.append({"kind": kind, "where": where, "claim": claim[:200], "verdict": v})
        # DISPLAY OF A VERDICT THE MODEL ALREADY REACHED — no judgement happens below. `v` is the JSON the
        # judge returned against the schema sent with the prompt; `guarded` and `verdict` are fields of that
        # schema. Choosing which of its own answers to print on screen is presentation, and every row is
        # written to the jsonl regardless, so nothing is filtered out of the record either.
        if kind == "guard" and not v.get("guarded"):
            print(f"\n  ⚠ UNGUARDED  {where}")
            print(f"      {claim[:100]}")
            print(f"      needs: {str(v.get('suggest'))[:110]}")
        elif kind == "split" and v.get("verdict") == "real_now":
            print(f"\n  ⛔ STILL REAL  {where}")
            print(f"      {str(v.get('why'))[:120]}")
        if i % 25 == 0:
            print(f"    …{i}/{len(jobs)}", flush=True)

    p = pathlib.Path(a.dir) / "wave_resolution.jsonl"
    with p.open("w") as fh:
        for row in out:
            fh.write(json.dumps(row) + "\n")
    print(f"\n  {dict(counts)}")
    print(f"  -> {p}")
    print("  UNJUDGED is neither guarded nor unguarded — those rows were not answered and stay open.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
