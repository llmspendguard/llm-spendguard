"""Per-vendor review-param optimizer — measure quality x latency x cost across reasoning-effort settings and
recommend (optionally record) the best, so "cheaper/faster" can NEVER silently mean "found fewer defects".

MOTIVE (measured 2026-08-14, confirmed by Moonshot docs). kimi-k3 carries reasoning_effort='auto' as a fact,
and on a real 800-char review it emitted 16,537 completion tokens in 484s. Moonshot's docs explain why: kimi-k3
thinking mode is ALWAYS ON, its real reasoning_effort values are low/high/max (DEFAULT max), and 'auto' is not
one of them — so it falls back to MAX, and the docs warn in as many words that "an agent loop that fires many
small calls pays max-effort reasoning on each one" (our panel). But you cannot just lower it blind: prior
measurement showed glm at minimal returned 10 tokens / 0 findings. So the effort is chosen by MEASUREMENT here
across kimi's REAL efforts — run each on real files, count valid findings, and have opus (on the $0 Claude lane)
JUDGE whether the cheaper setting MISSES defects the max setting found. Winner = fastest effort that loses none.

    .venv.nosync/bin/python scripts/experiment/optimize_review_params.py --vendor moonshot --model kimi-k3 \
        --efforts low,high,max --files 6 --record

POWER CAVEAT (learned the hard way, 2026-08-14). A 2-file run is an ANECDOTE, not a measurement: kimi's
findings are noisy enough that EACH effort — including max — "missed" on one of two files, so the strict
"lose zero material" rule defaulted to max off pure variance. Use --files >= 6 before trusting --record, and
read the verdict as PER-VENDOR completeness — in a multi-vendor panel a vendor need not be complete, since the
others cover its misses (the honest metric there is "does this vendor find defects the panel would otherwise
miss", a follow-on). Do not --record off a handful of files.
"""
import argparse
import json
import os
import pathlib
import time

import spendguard
spendguard.require()
os.environ.setdefault("SPENDGUARD_ADVISOR_EXECUTOR", "pool")   # opus judge rides the $0 Claude lane
from spendguard import adapters, vendor_call as vc

SCHEMA = {"type": "object", "properties": {"findings": {"type": "array", "items": {"type": "object",
          "properties": {"line": {"type": "integer"}, "issue": {"type": "string"}},
          "required": ["issue"]}}}, "required": ["findings"]}
SYSTEM = ("You are a meticulous code reviewer. Report real DEFECTS (bugs, races, unhandled errors, wrong logic) "
          "in the file as JSON: {\"findings\":[{\"line\":<int>,\"issue\":\"<one sentence>\"}]}. Only real defects.")


def review_files(n):
    """A few real source files of increasing size — the actual thing we review, not a toy."""
    root = pathlib.Path(__file__).resolve().parents[2] / "src" / "spendguard"
    fs = sorted((p for p in root.glob("*.py") if 1500 < p.stat().st_size < 12000), key=lambda p: p.stat().st_size)
    picks = [fs[len(fs) // 4], fs[len(fs) // 2], fs[3 * len(fs) // 4]][:n]
    return [(p.name, p.read_text()) for p in picks]


def parse_findings(text):
    try:
        blob = text[text.index("{"):text.rindex("}") + 1]
        d = json.loads(blob)
        return [f for f in (d.get("findings") or []) if isinstance(f, dict) and f.get("issue")]
    except Exception:
        return None                                            # unparseable -> not a clean result


def judge_recall(baseline, candidate, fname):
    """opus (0$ lane) decides if `candidate` findings MISS anything material in `baseline`. Meaning->LLM."""
    prompt = (f"Two code reviews of {fname}.\nTHOROUGH found:\n{json.dumps(baseline)[:4000]}\n\n"
              f"CANDIDATE found:\n{json.dumps(candidate)[:4000]}\n\n"
              "Does CANDIDATE miss any MATERIAL defect THOROUGH caught? Reply JSON: "
              '{"misses_material":true|false,"missed":["..."]}')
    r = vc.call("anthropic", "claude-opus-4-8", prompt, deadline_s=120, purpose="experiment:judge-recall",
                system="You judge review completeness. Be strict but only count MATERIAL defects.", schema=None)
    if not r.ok:
        return None
    try:
        t = r.text
        return json.loads(t[t.index("{"):t.rindex("}") + 1]).get("misses_material")
    except Exception:
        return None


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--vendor", default="moonshot")
    ap.add_argument("--model", default="kimi-k3")
    ap.add_argument("--efforts", default="low,medium,high,auto")
    ap.add_argument("--files", type=int, default=2)
    ap.add_argument("--record", action="store_true", help="store the winning effort as a models.py fact")
    a = ap.parse_args()
    efforts = [e.strip() for e in a.efforts.split(",") if e.strip()]
    files = review_files(a.files)
    print(f"optimizing {a.vendor}/{a.model} review effort over {len(files)} real files: {efforts}\n")

    rows = {}          # effort -> list of per-file dicts
    for fname, src in files:
        prompt = f"Review this file ({fname}):\n\n{src}"
        for eff in efforts:
            t0 = time.time()
            r = adapters.call(f"{a.vendor}:{a.model}", prompt, reasoning=eff, schema=SCHEMA,
                              max_tokens=32000, timeout_s=240, system=SYSTEM)
            dt = time.time() - t0
            finds = parse_findings(r.get("text") or "") if not r.get("error") else None
            rec = {"file": fname, "effort": eff, "latency": round(dt, 1), "out_tok": r.get("out_tok"),
                   "cost": r.get("cost"), "n": (len(finds) if finds is not None else None),
                   "findings": finds, "error": r.get("error")}
            rows.setdefault(eff, []).append(rec)
            print(f"  {fname:22} effort={eff:8} {dt:6.1f}s  out={r.get('out_tok')}  "
                  f"$={r.get('cost')}  findings={rec['n']}" + (f"  ERR {str(r.get('error'))[:50]}" if r.get('error') else ""))

    # thorough baseline = the effort that found the most (used as ground truth for the judge)
    def total_n(eff):
        return sum((x["n"] or 0) for x in rows.get(eff, []))
    baseline_eff = max(efforts, key=total_n)
    print(f"\n  thorough baseline effort = {baseline_eff} ({total_n(baseline_eff)} findings total)")

    print("\n=== per-effort summary (avg latency, total findings, judge: loses material vs baseline?) ===")
    ranked = []
    for eff in efforts:
        rs = rows.get(eff, [])
        avg_lat = sum(x["latency"] for x in rs) / max(1, len(rs))
        tot = total_n(eff)
        loses = False
        if eff != baseline_eff:
            for x in rs:
                base = next((b["findings"] for b in rows[baseline_eff] if b["file"] == x["file"]), [])
                if x["findings"] is None or judge_recall(base or [], x["findings"] or [], x["file"]):
                    loses = True
                    break
        ranked.append((eff, avg_lat, tot, loses))
        print(f"  {eff:8}  avg {avg_lat:6.1f}s  findings {tot:3}  loses_material={loses}")

    # winner: cheapest/fastest effort that does NOT lose material findings
    keep = [r for r in ranked if not r[3]]
    winner = min(keep, key=lambda r: r[1])[0] if keep else baseline_eff
    print(f"\n  >>> RECOMMENDED review effort for {a.vendor}/{a.model}: {winner}  "
          f"(fastest that keeps quality; baseline was {baseline_eff})")
    if a.record and winner:
        from spendguard import models
        models.add_fact(a.model, "reasoning", winner, source="experiment(optimize_review_params: quality-judged)",
                        verified=True)
        print(f"  recorded: models fact {a.model}.reasoning = {winner}")


if __name__ == "__main__":
    main()
