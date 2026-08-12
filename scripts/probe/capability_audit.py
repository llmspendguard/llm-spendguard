"""Find CONCEPTS implemented in more than one place — before an incident finds them.

WHY THIS EXISTS. On 2026-08-10 a guard test overwrote ~/.spendguard/config.json and destroyed every
setting in it. The proximate cause was the test. The real cause was that "write config.json" had FOUR
implementations (setup.py, chat.py x2, pricing.set_price), each with the same destructive read:

    try:    data = json.load(open(p))
    except: data = {}                  # an unreadable file becomes an empty one
    p.write_text(json.dumps(data))     # and is then replaced by it

A sweep afterwards found 29 whole-file JSON writes across 20 modules, exactly ONE of them atomic.

Five separate "one thing in one place" violations were fixed by hand that same day — models.apply_call_params,
pricing._vendor_qualified, reconcile.owner_ok, adapters.provider_for, the spend_audit writer — and every one
was found only because something broke. Fixing instances does not converge; the pattern outruns the fixes.

WHY IT IS AGENTIC. "Do these two functions do the same JOB?" is a question about meaning. `_write_state` and
`persist_snapshot` and `save_cache` share no tokens and may be the same capability; `record_cap` and
`record_effort` share a prefix and are not. Name similarity, call graphs and AST shape all answer a
different question than the one that matters, and each of them is the kind of mechanical proxy this
codebase forbids for exactly this reason.

WHAT IT DOES
  1. extracts every module-level and class-level function with its signature and first docstring line
  2. asks a model to group them by CAPABILITY — the job performed, not the words used
  3. reports every capability with more than one implementation, so the duplicate is a finding rather
     than an incident

Estimate-first and caged like everything else here. `--run` spends; without it you get the count and cost.
"""
import argparse, collections, json, os, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import repo_defs                                    # scope-qualified inventory — the ONE extractor

import spendguard                                   # noqa: F401
spendguard.require()
from spendguard import adapters, calls, config, pricing, ui       # noqa: E402

SYSTEM = (
    "You group functions by the JOB THEY DO, not by what they are called. Two functions belong to the same "
    "capability when a maintainer would say 'these are two implementations of the same thing and should be "
    "one'. Shared words in a name are NOT evidence; doing the same work to the same kind of data IS. "
    "Reading and writing the same resource are DIFFERENT capabilities. A thin wrapper that delegates is not "
    "a duplicate of what it delegates to. Be conservative: a false 'these are the same' sends someone to "
    "merge two things that should stay apart.")

SCHEMA = {"type": "object", "required": ["capabilities"], "properties": {"capabilities": {
    "type": "array", "items": {"type": "object", "required": ["capability", "functions"],
        "nonempty": ["capability"],
        "properties": {"capability": {"type": "string"},
                       "functions": {"type": "array", "items": {"type": "string"}},
                       "why_same": {"type": "string"}}}}}}

BATCH = 22          # BODIES are far larger than signatures; sized so the reply cannot be truncated


def candidates(root):
    """Definition sites with their BODIES. Names are identity only, never evidence of what a function does."""
    return repo_defs.defs(root)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default="src/spendguard")
    ap.add_argument("--model", default="")
    ap.add_argument("--run", action="store_true", help="without this, nothing is spent")
    ap.add_argument("--out", default="")
    a = ap.parse_args()
    os.environ["SPENDGUARD_ADVISOR_EXECUTOR"] = "api"

    fns = candidates(a.root)
    bad = repo_defs.unparsed(a.root)
    if bad:
        print(f"  ⚠ {len(bad)} file(s) UNPARSED — their functions are UNREVIEWED, not clean: "
              f"{', '.join(b[0] for b in bad[:3])}")
    # Private helpers count: config.json's four writers were all private. Nothing is filtered on a name.
    model = a.model or config.advisor_model()
    batches = [fns[i:i + BATCH] for i in range(0, len(fns), BATCH)]
    est = sum(pricing.realtime_cost(model, sum(len(x.src[:1400]) for x in b) // 4 + 300,
                                    25 * len(b) + 400) or 0 for b in batches)
    print(f"{len(fns)} functions in {a.root}, {len(batches)} judging call(s)")
    if not a.run:
        ui.estimate_only(action=f"group {len(fns)} functions by capability and flag the duplicates", cost=est)
        return 0

    caps = collections.defaultdict(list)
    for i, b in enumerate(batches, 1):
        # THE BODY IS THE EVIDENCE. Keyed on scope-qualified names so two same-named methods in different
        # classes are never conflated, and clustered on what the code DOES, not on what it is called.
        listing = "\n\n".join(f"### {x.qual}\n```python\n{x.src[:1400]}\n```" for x in b)
        prompt = (listing + "\n\nGroup these by CAPABILITY, judging ONLY from the bodies. Return ONLY capabilities implemented by MORE "
                  "THAN ONE function; omit every capability with a single implementation.\n"
                  'Reply JSON only: {"capabilities": [{"capability": "...", "functions": ["mod.fn", ...], '
                  '"why_same": "..."}]}')
        with calls.context(intent="spendguard:capability-audit"):
            # `25 * len(b) + 600` was a guess at how much a batch's reply would need, and a batch whose
            # groups ran long simply lost the tail — capabilities that WERE duplicated dropped off the end
            # of a truncated JSON array and read as "no duplicates in this batch".
            r = adapters.call_complete(model, prompt, sig="probe:capability-audit", system=SYSTEM)
        if r.get("error"):
            print(f"  batch {i}/{len(batches)}: FAILED ({str(r['error'])[:70]}) — its functions are UNJUDGED, "
                  f"not 'no duplicates'")
            continue
        try:
            blob = re.search(r"\{.*\}", r.get("text") or "", re.S)
            for c in (json.loads(blob.group(0)).get("capabilities") if blob else []) or []:
                if len(c.get("functions") or []) > 1:
                    caps[c["capability"]].append(c)
        except Exception:
            print(f"  batch {i}/{len(batches)}: unparseable reply — its functions are UNJUDGED")
        print(f"  batch {i}/{len(batches)} judged", flush=True)

    print(f"\n  CAPABILITIES WITH MORE THAN ONE IMPLEMENTATION: {len(caps)}")
    rows = []
    for cap, items in sorted(caps.items(), key=lambda kv: -sum(len(i["functions"]) for i in kv[1])):
        fnames = sorted({f for i in items for f in i["functions"]})
        why = next((i.get("why_same") for i in items if i.get("why_same")), "")
        print(f"    {cap}  ({len(fnames)})")
        print(f"      {', '.join(fnames)}")
        if why:
            print(f"      why: {why[:120]}")
        rows.append({"capability": cap, "functions": fnames, "why_same": why})
    out = a.out or os.path.join(str(config.HOME), "capability_audit.jsonl")
    with open(out, "w") as fh:
        for r_ in rows:
            fh.write(json.dumps(r_) + "\n")
    print(f"\n  rows -> {out}")
    print("  A capability with two implementations is a finding, not a style note: on 2026-08-10 one of "
          "them destroyed a user's settings file.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
