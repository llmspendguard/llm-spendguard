"""Judge review findings against the REAL code, with two independent validators, before anything is fixed.

WHY THIS STANDS BETWEEN REVIEW AND REPAIR. A four-vendor review of 23 files produced 374 findings across 325
sites. Fixing 374 unverified claims would be a large, risky change driven mostly by noise — and a wrong "fix"
to working code is worse than the finding it answered, because it arrives with a rationale attached.

WHAT COUNTS AS A CANDIDATE. Only sites more than one vendor reached independently. Cross-vendor agreement is
the cheapest evidence available that a finding describes the code rather than the reviewer, and it is free:
it falls out of having asked four. Single-vendor findings are LEADS and stay in the file, unfixed.

THE VALIDATORS SEE THE ACTUAL SOURCE, not the review. Each is given the real lines around the cited number
and asked to rule on the claim itself. A validator shown only the finding would be grading prose.

TWO OF TWO, OR IT IS NOT FIXED. opus and gpt-5.5 rule independently; disagreement is REPORTED, never
averaged into a score and never resolved by picking the more confident one. Two models that disagree about
whether code is broken is exactly the case a human should see.

Line numbers are trustworthy here because source_compact preserves them — that was not true earlier today,
and every finding from that run pointed at the wrong code while looking perfectly plausible.
"""
import argparse, collections, json, os, pathlib, re, sys

import spendguard                                   # noqa: F401
spendguard.require()
from spendguard import config, vendor_call as vc    # noqa: E402

VALIDATORS = [("anthropic", "claude-opus-4-8"), ("openai", "gpt-5.5")]
CONTEXT_LINES = 25

SYSTEM = ("You are validating a claimed code defect against the real source. Be strict and literal. "
          "A claim is REAL only if the code as written can actually produce the described failure. "
          "Reject claims that are stylistic, speculative, already handled elsewhere in the shown code, or "
          "that misread what the code does. You are not being asked to improve the code.")

VERDICT_SCHEMA = {
    "type": "object", "required": ["real", "why", "fix"],
    "nonempty": ["why"],
    "properties": {"real": {"type": "boolean"},
                   "why": {"type": "string"},
                   "fix": {"type": "string"},
                   "severity": {"type": "string"}}}


# How far apart two findings may sit and still be worth ASKING about. Not a decision — a recall filter on
# a provable quantity (line distance), which only narrows what gets sent to the judge. The judge decides.
NEAR_LINES = 40

# Pairs per judging call. Sized so the reply cannot approach an output ceiling: a truncated JSON body
# parses to nothing, and "nothing merged" reads identically to "nothing was the same".
BATCH_PAIRS = 120

_SYS_SAME_DEFECT = (
    "You group code-review findings. Two findings are THE SAME DEFECT only if they describe the same "
    "underlying fault in the same code — not merely the same file, the same function, or the same general "
    "topic. Two different bugs a few lines apart are NOT the same defect. When unsure, say they differ: "
    "merging two distinct findings invents an agreement that nobody actually reached.")


def merge_near_misses(sites, run=False, model=None):
    """Group findings that describe ONE defect but were reported at slightly different lines.

    WHY THIS EXISTS. Grouping was exact equality on (file, line), and vendors' line numbers are only
    approximate: on the `text_tokens or ...` defect two vendors cited line 305 for code at 329, and on
    saas.py two vendors both cited 155 for a claim about code that is not in that file at all. Exact
    matching is therefore wrong in both directions and both are invisible:
      MISSED    the same defect reported at 305 and 329 does not group, so each looks like a lone lead and
                is never validated — the likeliest reason wave 2 turned 184 findings into 5 sites
      INVENTED  two DIFFERENT defects that both land on 155 group into an agreement nobody reached, which
                is precisely what saas.py:155 was

    WHETHER TWO FINDINGS ARE THE SAME DEFECT IS A JUDGEMENT, so a model makes it. Line distance is used
    only to decide what is worth ASKING about — a recall filter on a provable quantity, not the answer.
    Nothing is merged without a model saying so, and `run=False` merges nothing at all."""
    from spendguard import adapters, calls, config, ui
    by_file = collections.defaultdict(list)
    for k, items in sites.items():
        by_file[k[0]].append((k[1], items))
    pairs = []
    for f, entries in by_file.items():
        entries.sort()
        for i, (ln_a, items_a) in enumerate(entries):
            for ln_b, items_b in entries[i + 1:]:
                if ln_b - ln_a > NEAR_LINES:
                    break
                if {x["vendor"] for x in items_a} & {x["vendor"] for x in items_b}:
                    continue          # the same vendor twice is not cross-vendor agreement
                pairs.append((f, ln_a, ln_b, items_a[0]["issue"], items_b[0]["issue"]))
    if not pairs:
        return sites, {"pairs": 0, "merged": 0}
    m = model or config.advisor_model()
    if not run:
        ui.estimate_only(action=f"judge {len(pairs)} near-miss finding pair(s) for sameness", cost=None)
        return sites, {"pairs": len(pairs), "merged": 0, "judged": False}
    # BATCHED. Wave 1 produced 1,145 pairs — one call would be a ~132K-token prompt asking for ~23K tokens
    # of output, which is over every ceiling here and would come back TRUNCATED. A truncated JSON body
    # parses to nothing, and "nothing merged" is indistinguishable from "nothing was the same".
    same, judged, failed = set(), 0, 0
    for start in range(0, len(pairs), BATCH_PAIRS):
        chunk = pairs[start:start + BATCH_PAIRS]
        listing = "\n".join(
            f"{start + j}. {pathlib.Path(f).name}\n   A (line {a}): {ia[:220]}\n   B (line {b}): {ib[:220]}"
            for j, (f, a, b, ia, ib) in enumerate(chunk))
        prompt = (listing + '\n\nFor each numbered pair, do A and B describe the SAME defect?\n'
                            'Reply JSON only: {"pairs": [{"i": <index>, "same": true|false}]}')
        with calls.context(intent="review:group-near-miss-findings"):
            r = adapters.call(m, prompt, max_tokens=20 * len(chunk) + 400, system=_SYS_SAME_DEFECT)
        if r.get("error"):
            failed += len(chunk)
            continue
        try:
            blob = re.search(r"\{.*\}", r.get("text") or "", re.S)
            for it in (json.loads(blob.group(0)).get("pairs") if blob else []) or []:
                if it.get("same"):
                    same.add(int(it["i"]))
            judged += len(chunk)
        except Exception:
            failed += len(chunk)
    if failed:
        # A BATCH THAT DID NOT ANSWER IS NOT A BATCH THAT SAID "ALL DIFFERENT". Its pairs stay ungrouped,
        # which is the same outcome as the old exact-line matching — but it must be VISIBLE, or the run
        # reports a smaller undercount than it actually has.
        print(f"  near-miss grouping: {failed} pair(s) went UNJUDGED (the call failed) and stay ungrouped")
    merged = 0
    for i, (f, a, b, _ia, _ib) in enumerate(pairs):
        if i not in same:
            continue
        ka, kb = (f, a), (f, b)
        if ka in sites and kb in sites:
            sites[ka].extend(sites.pop(kb))          # keep the EARLIER line as the anchor
            merged += 1
    return sites, {"pairs": len(pairs), "merged": merged, "judged": True}


def candidates(path, min_vendors=2, only_files=None, group=False, group_model=None):
    rows = [json.loads(x) for x in open(path) if x.strip()]
    # AGREEMENT NEEDS A DENOMINATOR. "2 vendors agreed" means something different when 4 were asked than
    # when 3 could answer — and on the largest files fewer can: Moonshot refuses source payloads over
    # roughly 17,000 chars, so a big module gets three reviewers while this gate assumes four. Coverage
    # rows carry who was actually able to review each file; a file with none simply has no denominator
    # recorded (older runs), which is reported as unknown rather than assumed to be the full panel.
    coverage = {r["file"]: r for r in rows if r.get("kind") == "coverage"}
    sites = collections.defaultdict(list)
    for r in rows:
        if not isinstance(r.get("line"), int):
            continue
        if only_files and pathlib.Path(r["file"]).name not in only_files:
            continue
        sites[(r["file"], r["line"])].append(r)
    if group:
        sites, gstats = merge_near_misses(sites, run=True, model=group_model)
        print(f"  near-miss grouping: {gstats['pairs']} pair(s) within {NEAR_LINES} lines judged, "
              f"{gstats['merged']} merged as the same defect")
    out = []
    for (f, line), items in sites.items():
        vendors = sorted({i["vendor"] for i in items})
        if len(vendors) < min_vendors:
            continue
        cov = coverage.get(f) or {}
        out.append({"file": f, "line": line, "vendors": vendors,
                    "reviewers": cov.get("reviewers"),            # None = no coverage recorded for this run
                    "unavailable": cov.get("unavailable") or {},
                    "claims": [i["issue"] for i in items][:4],
                    "severity": max((i.get("severity") or "") for i in items)})
    return sorted(out, key=lambda c: -len(c["vendors"]))


def snippet(path, line, n=CONTEXT_LINES):
    src = pathlib.Path(path).read_text().splitlines()
    lo, hi = max(0, line - n), min(len(src), line + n)
    return "\n".join(f"{i+1:>5}| {src[i]}" for i in range(lo, hi))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--findings", default="")
    ap.add_argument("--min-vendors", type=int, default=2)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--budget", type=float, default=8.0)
    ap.add_argument("--estimate", action="store_true")
    ap.add_argument("--group", action="store_true",
                    help="agentically merge findings that describe ONE defect but were reported at "
                         "slightly different lines. Exact-line matching found 5 sites in wave 2; grouping "
                         "found 35 — a 7x undercount, because vendors' line numbers are approximate.")
    ap.add_argument("--out", default="")
    a = ap.parse_args()
    os.environ["SPENDGUARD_ADVISOR_EXECUTOR"] = "api"

    src = a.findings or os.path.join(str(config.HOME), "repo_review.jsonl")
    cands = candidates(src, a.min_vendors, group=a.group)
    if a.limit:
        cands = cands[:a.limit]
    if not cands:
        print("no multi-vendor candidates")
        return 1
    print(f"{len(cands)} candidate sites ({a.min_vendors}+ vendors agreeing) from {src}")

    if a.estimate:
        from spendguard import pricing, expected_output
        tot = 0.0
        for v, m in VALIDATORS:
            pred, _b = expected_output.expect(m)
            tot += sum((pricing.realtime_cost(m, 900, min(pred or 600, 900)) or 0) for _ in cands)
        print(f"\nZERO-SPEND ESTIMATE — {len(cands)*len(VALIDATORS)} validations, ~${tot:,.2f} "
              f"(hard budget ${a.budget:,.2f})")
        return 0

    out_path = a.out or os.path.join(str(config.HOME), "validated_findings.jsonl")
    fh = open(out_path, "w")
    confirmed, rejected, split, unvalidated = [], [], [], []
    print(f"\n  {'file:line':<34}{'vendors':>8}  opus / gpt-5.5   verdict")
    for c in cands:
        code = snippet(c["file"], c["line"])
        prompt = (f"FILE: {pathlib.Path(c['file']).name}\nCLAIMED DEFECT AT LINE {c['line']}:\n"
                  + "\n".join(f"  - {x}" for x in c["claims"])
                  + f"\n\nACTUAL SOURCE (line numbers are real):\n{code}\n\n"
                  "Is this a real defect in the code as written? Reply JSON only: "
                  '{"real": true|false, "why": "<one sentence>", "fix": "<one line, or empty if not real>", '
                  '"severity": "high|medium|low"}')
        verdicts = {}
        for v, m in VALIDATORS:
            # An EMPTY response (HTTP 200, zero characters) is a reasoning model spending its budget on
            # thinking and leaving nothing for the answer. It is stochastic, not deterministic — measured
            # here at 2 of 53 for gpt-5.5 — so it is worth exactly one retry. Every other failure kind is
            # deterministic and retrying it burns money to reach the same place.
            for attempt in (1, 2):
                r = vc.call(v, m, prompt, deadline_s=300, purpose="review:validate-finding",
                            system=SYSTEM, schema=VERDICT_SCHEMA)
                if r.kind != "empty":
                    break
            try:
                verdicts[m] = json.loads(r._text) if r.ok and isinstance(r._text, str) else None
            except Exception:
                verdicts[m] = None
        vals = [verdicts.get(m) for _v, m in VALIDATORS]
        says = [bool(x.get("real")) if x else None for x in vals]
        if any(s is None for s in says):
            # A VALIDATOR THAT DID NOT ANSWER IS NOT A VALIDATOR THAT DISAGREED. Bucketing a failed call as
            # a "split decision" invents a disagreement out of an absence and makes the gate look more
            # contested than it is — 3 of the first 9 "splits" were simply gpt-5.5 returning nothing.
            bucket, mark = unvalidated, "UNVALIDATED — a validator did not answer"
        elif all(s is True for s in says):
            bucket, mark = confirmed, "CONFIRMED"
        elif all(s is False for s in says):
            bucket, mark = rejected, "rejected"
        else:
            # NOT averaged and NOT decided by the more confident one. Two strong models disagreeing about
            # whether code is broken is precisely the case a person should look at.
            bucket, mark = split, "SPLIT — needs a human"
        rec = {**c, "verdicts": {m: verdicts.get(m) for _v, m in VALIDATORS}, "outcome": mark}
        bucket.append(rec)
        fh.write(json.dumps(rec) + "\n"); fh.flush()
        shown = "/".join("yes" if s else ("no" if s is False else "?") for s in says)
        # "2/4" and "2/3" are different strengths of evidence and must not print identically.
        denom = len(c["reviewers"]) if c.get("reviewers") else None
        agree = f"{len(c['vendors'])}/{denom}" if denom else f"{len(c['vendors'])}/?"
        print(f"  {pathlib.Path(c['file']).name+':'+str(c['line']):<34}{agree:>8}  {shown:<16}{mark}",
              flush=True)
    fh.close()

    print(f"\n  CONFIRMED by BOTH: {len(confirmed)}   rejected: {len(rejected)}   "
          f"genuine SPLIT: {len(split)}   UNVALIDATED (a validator did not answer): {len(unvalidated)}")
    print("\n  THE FIX LIST (both validators agree the code is broken):")
    for c in sorted(confirmed, key=lambda x: -len(x["vendors"])):
        v = c["verdicts"].get("claude-opus-4-8") or {}
        print(f"    {pathlib.Path(c['file']).name}:{c['line']}  [{v.get('severity','?')}] "
              f"{(v.get('why') or '')[:96]}")
        if v.get("fix"):
            print(f"        fix: {v['fix'][:100]}")
    if split:
        print("\n  SPLIT — validators disagree, NOT fixed without you:")
        for c in split[:8]:
            a_ = c["verdicts"].get("claude-opus-4-8") or {}
            b_ = c["verdicts"].get("gpt-5.5") or {}
            print(f"    {pathlib.Path(c['file']).name}:{c['line']}")
            print(f"        opus   : {'REAL' if a_.get('real') else 'not real'} — {(a_.get('why') or '')[:80]}")
            print(f"        gpt-5.5: {'REAL' if b_.get('real') else 'not real'} — {(b_.get('why') or '')[:80]}")
    print(f"\n  rows -> {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
