"""Review a CONCEPT across every file that implements it — the defect class a per-file review cannot see.

WHY THIS EXISTS. repo_review_panel.py reviews ONE FILE per reviewer prompt (see its read loop). That shape
can only find defects that fit inside one file. Three waves, four vendors, ~500 findings, and not a single
"this concept is implemented twice" — because the second implementation was never in the reviewer's context.
It was not a diligence failure; the evidence was absent. Meanwhile config.json had FOUR writers, three of
them destructive, and the way that was finally discovered was by destroying a user's settings file.

So the unit of review here is the CAPABILITY, not the file. capability_audit.py finds concepts implemented in
more than one place; this reads the FULL SOURCE of every implementation, puts them in ONE context, and asks
the question that only makes sense with all of them side by side:

    THESE ARE ALL SUPPOSED TO DO THE SAME THING. HAVE THEY DRIFTED, AND WHICH ONE IS NOW WRONG?

Drift is the payload. Two identical copies are a maintenance smell. Two copies that have DIVERGED means one of
them has a bug the other one already fixed — which is exactly what config.json was: one writer had been made
atomic, three had not, and the three still carried `except: data = {}`, turning an unreadable file into an
empty one. A reviewer holding all four at once would have seen it in seconds.

CLI: review_capability_slice.py [--top N] [--run].  Estimate-first; caged like everything else.
"""
import argparse, ast, collections, json, os, pathlib, re, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import repo_defs                                    # the ONE scope-qualified inventory

import spendguard                                     # noqa: F401
spendguard.require()
from spendguard import adapters, calls, config, pricing, ui        # noqa: E402

SYSTEM = (
    "You are shown EVERY implementation of one capability in a codebase, in full. Your job is not style. Your "
    "job is DRIFT: these are supposed to do the same thing, so find where they DISAGREE, and say which one is "
    "WRONG. A difference in error handling, in what happens on a missing/unreadable input, in locking, in "
    "atomicity, or in what is returned for the empty case is a BUG in whichever copy is behind — the other copy "
    "is the evidence of what correct looks like. If they genuinely do different jobs, say so plainly and stop; "
    "a false merge is worse than a duplicate.")

SCHEMA_HINT = ('{"same_job": true|false, "canonical": "mod.fn", "drifted": true|false, '
               '"divergences": [{"where": "mod.fn", "behaviour": "what it does differently", '
               '"wrong": true|false, "impact": "what breaks because of it"}], "merge": "how to unify"}')


def sources(root):
    """{scope-qualified name: full source}. Delegates to repo_defs — the flat `module.name` key this used to
    build collided 11 times, so the reviewer read one body and attributed it to another definition."""
    return {d.qual: d.src for d in repo_defs.defs(root)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--audit", default=os.path.join(str(config.HOME), "capability_audit.jsonl"))
    ap.add_argument("--root", default="src/spendguard")
    ap.add_argument("--top", type=int, default=0, help="0 = every cross-file capability")
    ap.add_argument("--model", default="")
    ap.add_argument("--run", action="store_true")
    ap.add_argument("--out", default="")
    a = ap.parse_args()
    os.environ["SPENDGUARD_ADVISOR_EXECUTOR"] = "api"

    src = sources(a.root)
    rows = [json.loads(l) for l in open(a.audit)]
    # CROSS-FILE FIRST — those are the ones no existing review could have found. Same-file duplicates were at
    # least VISIBLE to the per-file panel, so they are not the blind spot being closed here.
    slices = [r for r in rows if len({f.split(".")[0] for f in r["functions"]}) > 1]
    slices.sort(key=lambda r: -len({f.split(".")[0] for f in r["functions"]}))
    if a.top:
        slices = slices[:a.top]

    def body(r):
        parts = []
        for fn in r["functions"]:
            code = src.get(fn)
            parts.append(f"### {fn}\n```python\n{code}\n```" if code
                         else f"### {fn}\n(SOURCE NOT FOUND — treat as UNREVIEWED, not as absent)")
        return f"CAPABILITY: {r['capability']}\n\n" + "\n\n".join(parts)

    model = a.model or config.advisor_model()
    est = sum(pricing.realtime_cost(model, len(body(r)) // 4 + 400, 900) or 0 for r in slices)
    print(f"{len(slices)} cross-file capabilities · {sum(len(r['functions']) for r in slices)} implementations")
    if not a.run:
        ui.estimate_only(action=f"review {len(slices)} capability slices for DRIFT across files", cost=est)
        return 0

    out_rows, drifted = [], 0
    for i, r in enumerate(slices, 1):
        prompt = (body(r) + "\n\nThese are all implementations of the SAME capability. Have they DRIFTED, and "
                  f"which one is now WRONG?\nReply JSON only: {SCHEMA_HINT}")
        with calls.context(intent="spendguard:capability-slice-review"):
            resp = adapters.call(model, prompt, max_tokens=1400, system=SYSTEM)
        if resp.get("error"):
            print(f"  {i}/{len(slices)} {r['capability'][:50]}: FAILED — UNREVIEWED, not clean")
            out_rows.append({**r, "verdict": None, "why": str(resp["error"])[:120]})
            continue
        try:
            blob = re.search(r"\{.*\}", resp.get("text") or "", re.S)
            v = json.loads(blob.group(0)) if blob else None
        except Exception:
            v = None
        if v is None:
            print(f"  {i}/{len(slices)} {r['capability'][:50]}: unparseable — UNREVIEWED")
            out_rows.append({**r, "verdict": None, "why": "unparseable"})
            continue
        bad = [d for d in (v.get("divergences") or []) if d.get("wrong")]
        if v.get("same_job") and v.get("drifted") and bad:
            drifted += 1
            print(f"\n  ⚠ DRIFTED  {r['capability']}")
            print(f"      canonical: {v.get('canonical')}")
            for d in bad:
                print(f"      ✗ {d.get('where')} — {str(d.get('behaviour'))[:100]}")
                print(f"          impact: {str(d.get('impact'))[:100]}")
        else:
            state = "same job, no wrong divergence" if v.get("same_job") else "DIFFERENT jobs — leave apart"
            print(f"  {i}/{len(slices)} {r['capability'][:52]}: {state}")
        out_rows.append({**r, "verdict": v})

    out = a.out or os.path.join(str(config.HOME), "capability_drift.jsonl")
    with open(out, "w") as fh:
        for r_ in out_rows:
            fh.write(json.dumps(r_) + "\n")
    unrev = sum(1 for r_ in out_rows if r_.get("verdict") is None)
    print(f"\n  {drifted} capabilities have DRIFTED with a wrong copy · {unrev} UNREVIEWED (not clean) -> {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
