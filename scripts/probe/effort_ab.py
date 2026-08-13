"""Settle reasoning EFFORT per (call-class, model) by running it, and record the verdict.

WHY. Effort is the third bound alongside max_tokens and the deadline, and the only one still chosen by hand.
It is worth settling because reasoning is billed as OUTPUT and output is where the money is: measured over a
four-vendor code review, 91% of the bill was output and 92-98% of that output was reasoning nobody sees —
glm-5.2 emitted 11,176 tokens per call to deliver 262 tokens of findings.

It cannot be reasoned about from the outside. Two things the first cost-only measurement got wrong:
  * glm-5.2 at `minimal` returned 10 output tokens and ZERO findings. A 96x "saving" that had stopped working.
  * kimi-k3 found MORE at `minimal` than at `high` on pricing.py (3 vs 2). Effort is not monotonic.

WHO DECIDES WHAT
  the vendor      which tiers exist          — discovered (vendor_call.discover_efforts), never a literal
  an LLM          which tier to prefer       — the JUDGE, below: "did the cheaper arm find the same defects"
  measurement     whether the preference holds — arms on real inputs, repeated
  models.add_fact the verdict, with provenance — the existing per-model learning store, not a new one

THE JUDGE IS AGENTIC ON PURPOSE. equivalence.grade defaults to `auto`, which for free-text findings falls
through to a text-similarity ratio — a string proxy for "did it find the same bugs", and exactly the
regex-grade substitution that would make this whole run worthless. mode='rubric' is a caged LLM comparison.
Counting findings would be worse still: kimi-k3's 3-at-minimal vs 2-at-high says nothing about whether the
3 include the real defect.

THE REFERENCE. call_io has no review-intent samples, so the top tier establishes the reference on the same
inputs and every cheaper arm is graded against it. That doubles nothing — the top tier is an arm we wanted
anyway — but it does mean the reference is our own best effort, not a human's, and the verdict is
"as good as our best", never "correct".

SAFETY: --estimate is a separate zero-spend pass; --budget is a hard stop checked against the LEDGER, not
against our own counter (measured: a counter that could not see retries reported $1.98 against $13.12).
"""
import argparse, json, os, pathlib, statistics, sys

import spendguard                                   # noqa: F401
spendguard.require()
from spendguard import config, equivalence, models as model_facts, pricing, vendor_call as vc   # noqa: E402
from spendguard.source_compact import compact                                                   # noqa: E402

PANEL = [("moonshot", "kimi-k3"), ("zai", "glm-5.2")]
INTENT = "review:code-defects"
SYSTEM = ("You are a code reviewer. Be terse: no preamble, no restatement of the code, no summary. "
          "Report only real correctness defects you can point at a line for.")
SCHEMA = {"type": "object", "required": ["issues"], "properties": {"issues": {"type": "array", "items": {
    "type": "object", "required": ["line", "issue"], "nonempty": ["issue"],
    "properties": {"line": {"type": "integer"}, "issue": {"type": "string"}}}}}}

# A tier only counts as equivalent at or above this. Not a hand-picked quality bar dressed as a constant:
# it is the DECISION THRESHOLD, stated on the CLI, printed with every verdict, and the raw scores are always
# shown so the reader can disagree with it.
DEFAULT_KEEP = 0.85


def samples(k, lo=2000, hi=14000):
    """Real modules, compacted, in the size band the review actually runs on.

    Ordered LARGEST FIRST: a bigger module is likelier to contain something to find, and a file the
    reference reviews as clean contributes nothing but cost — every tier agrees on an empty answer."""
    out = []
    files = sorted(pathlib.Path("src/spendguard").glob("*.py"),
                   key=lambda f: -len(f.read_text()))
    for f in files:
        src = f.read_text()
        if not (lo <= len(src) <= hi):
            continue
        body, st = compact(src)
        if st.get("ok"):
            out.append({"name": f.name, "src": body})
        if len(out) >= k:
            break
    return out


def ledger_total():
    import sqlite3
    try:
        c = sqlite3.connect(config.db_path())
        return float(c.execute("SELECT COALESCE(SUM(cost),0) FROM calls").fetchone()[0] or 0)
    except Exception:
        return -1.0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--files", type=int, default=5)
    ap.add_argument("--keep", type=float, default=DEFAULT_KEEP,
                    help="equivalence at or above which a cheaper tier is accepted (printed with every verdict)")
    ap.add_argument("--budget", type=float, default=15.0, help="HARD stop in $, checked against the LEDGER")
    ap.add_argument("--judge-model", default="claude-haiku-4-5")
    ap.add_argument("--estimate", action="store_true")
    a = ap.parse_args()
    os.environ["SPENDGUARD_ADVISOR_EXECUTOR"] = "api"

    fs = samples(a.files)
    plan = []
    for v, m in PANEL:
        d = vc.discover_efforts(v, m)                       # DISCOVERED, never a literal
        tiers = [t for t in (d.get("accepted") or [])]
        if not tiers:
            print(f"  {v}/{m}: no discovered tiers — skipping rather than guessing")
            continue
        plan.append((v, m, tiers))
    if not plan or not fs:
        print("nothing to test")
        return 1

    print(f"{len(fs)} real modules: " + ", ".join(f['name'] for f in fs))
    for v, m, tiers in plan:
        print(f"  {v}/{m:<12} tiers: {','.join(tiers)}")

    if a.estimate:
        # OUTPUT COMES FROM expected_output, NOT A LITERAL — and this is a QUOTE, ruled so by
        # `spendguard estimate-literals`: the number below is printed as dollars and compared against a
        # hard budget, so it authorizes the run. It read `3000` for the arm output and `1500, 200` for the
        # judge, all invented. On a reasoning model that omission is the whole error: hidden thinking is
        # billed as output, which is how a $34 quote became a ~$380 run. expect() returns the count and the
        # basis it came from, so the printed figure can say which.
        from spendguard import expected_output
        tot = 0.0
        for v, m, tiers in plan:
            arm_out, _basis = expected_output.expect(m, sig=f"probe:effort_ab:{m}")
            for f in fs:
                for _t in tiers:
                    tot += pricing.realtime_cost(m, len(f["src"]) // 4, arm_out) or 0
        judge = len(fs) * sum(len(t) - 1 for _v, _m, t in plan)
        # The judge's INPUT is the rubric plus two arm outputs — measurable from what we already know,
        # rather than the flat 1500 that stood in for it.
        judge_in = max(1, len(SYSTEM) // 4) + 2 * arm_out
        judge_out, _jb = expected_output.expect(a.judge_model, sig="probe:effort_ab:judge")
        jc = (pricing.realtime_cost(a.judge_model, judge_in, judge_out) or 0) * judge
        print(f"\nZERO-SPEND ESTIMATE — {sum(len(t) for _v,_m,t in plan) * len(fs)} arm calls ${tot:,.2f}"
              f" + {judge} judge calls ${jc:,.2f}  =  ${tot+jc:,.2f}  (hard budget ${a.budget:,.2f})")
        return 0

    out_path = os.path.join(str(config.HOME), "effort_ab.jsonl")
    fh = open(out_path, "a")
    t0 = ledger_total()
    rows = []
    print(f"\n  {'model':<12}{'tier':<9}{'file':<22}{'out tok':>9}{'$':>9}{'equiv':>8}  vs reference")
    for v, m, tiers in plan:
        top = tiers[-1]                                     # the reference arm: our own best effort
        for f in fs:
            if ledger_total() - t0 > a.budget:
                print(f"  BUDGET STOP at ${ledger_total()-t0:,.2f}")
                break
            ref_text, ref_cost, ref_out = None, 0.0, 0
            ref_findings = 0
            for tier in [top] + [t for t in tiers if t != top]:
                r = vc.call(v, m, f"Review this file for defects: {f['name']}\n\n{f['src']}",
                            deadline_s=300, purpose=f"{INTENT}:effort-ab", system=SYSTEM,
                            schema=SCHEMA, reasoning=tier)
                txt = r._text if r.ok else ""
                if tier == top:
                    ref_text, ref_cost, ref_out = txt, (r.cost or 0), r.out_tok
                    eq = 1.0
                    # A FILE THAT YIELDS NO FINDINGS CANNOT DISCRIMINATE BETWEEN TIERS. Every arm returns
                    # {"issues":[]}, every comparison scores 1.0, and the verdict becomes "the cheapest tier
                    # is always fine" — drawn from a test that could not have said anything else. Note that
                    # out_tok is NOT the tell: a tier can burn 985 tokens of hidden reasoning to arrive at
                    # the same empty answer as one that spent 15, which is exactly what happened first time.
                    try:
                        ref_findings = len((json.loads(txt) or {}).get("issues") or []) if txt else 0
                    except Exception:
                        ref_findings = 0
                    if ref_findings == 0:
                        print(f"  {m:<12}{tier:<9}{f['name'][:20]:<22}{r.out_tok:>9,}"
                              f"${(r.cost or 0):>8.4f}{'—':>8}  SKIP: reference found nothing to compare on",
                              flush=True)
                        break
                else:
                    # AGENTIC JUDGE. rubric, not the default auto ladder — free-text findings would fall to a
                    # string-similarity ratio, which cannot tell "found the same bug" from "used the same words".
                    eq = equivalence.grade(ref_text or "", txt, mode="rubric", model=a.judge_model)[0] \
                        if (ref_text and txt) else 0.0
                row = {"vendor": v, "model": m, "tier": tier, "file": f["name"], "kind": r.kind,
                       "out_tok": r.out_tok, "cost": round(r.cost or 0, 6), "equiv": round(eq, 3),
                       "is_reference": tier == top}
                rows.append(row); fh.write(json.dumps(row) + "\n"); fh.flush()
                print(f"  {m:<12}{tier:<9}{f['name'][:20]:<22}{r.out_tok:>9,}${(r.cost or 0):>8.4f}"
                      f"{eq:>8.2f}  {'(reference)' if tier == top else ''}", flush=True)
    fh.close()

    print(f"\n  VERDICT — cheapest tier holding equivalence >= {a.keep:.2f} vs our own best effort")
    for v, m, tiers in plan:
        best = None
        for tier in tiers:
            cells = [r for r in rows if r["model"] == m and r["tier"] == tier and r["kind"] == "ok"]
            if not cells:
                continue
            eq = statistics.median(r["equiv"] for r in cells)
            cost = statistics.mean(r["cost"] for r in cells)
            mark = "PASS" if eq >= a.keep else "below bar"
            print(f"    {m:<12}{tier:<9}equiv {eq:>5.2f}  ${cost:>7.4f}/file   {mark}")
            if eq >= a.keep and (best is None or cost < best[1]):
                best = (tier, cost, eq)
        if best:
            model_facts.add_fact(m, "reasoning", best[0], confidence=0.9,
                                 source=f"effort_ab n={len(fs)} equiv={best[2]:.2f} keep>={a.keep}",
                                 verified=True)
            print(f"    -> {m}: RECORDED reasoning={best[0]} (${best[1]:.4f}/file, equiv {best[2]:.2f})")
        else:
            print(f"    -> {m}: NO tier met the bar — recording nothing rather than a guess")
    print(f"\n  ledger delta ${ledger_total()-t0:,.2f}   rows -> {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
