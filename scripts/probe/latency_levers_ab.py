"""Which lever actually makes a slow vendor fast: constraining the OUTPUT, or picking a lighter MODEL?

WHY THIS EXISTS. "z.ai and moonshot are slow" turned out to be the wrong description. Decomposing 30 days of
real calls into output size x generation speed:

    glm-5.2    1,816 median output tokens   21.1s   81.8 tok/s   <- FASTEST per token of the four
    kimi-k3    1,201                        30.6s   37.8 tok/s
    gpt-5.5      512                        11.6s   53.7 tok/s
    opus-4-8     126                         2.7s   44.6 tok/s

z.ai is not slow, it is VERBOSE. Latency is dominated by how many tokens a model chooses to emit, and the
two "slow" vendors emit 10-14x what opus does for comparable work. That makes output length the lever, and
it is free: the same constraint cuts latency AND output cost together.

The second lever is the model itself — both vendors serve lighter siblings (glm-5-turbo, glm-4.5-air,
kimi-k2.7-code-highspeed) that discovery confirms exist, rather than ids anyone guessed.

This probe measures both against the SAME real review prompt, N times each, because a single call cannot
separate a fast model from a lucky draw. It reports tok/s alongside latency: a model that is slower only
because it said more is a prompting problem, and a model that is slower per token is a model choice.
"""
import argparse, json, os, statistics, sys

import spendguard                                   # noqa: F401
spendguard.require()
from spendguard import config, pricing, vendor_call as vc   # noqa: E402

# Confirmed served by list_models() — never a guessed id.
# kimi-k3 and glm-5.2 are the REVIEW models and stay. The lighter siblings are measured only to price the
# quality trade honestly — k2.7 is a weaker reviewer than k3, so "it is faster" is not on its own an argument
# for using it on review work. Keep them for classification-grade jobs, if at all.
CANDIDATES = [("zai", "glm-5.2"), ("zai", "glm-5-turbo"),
              ("moonshot", "kimi-k3"), ("moonshot", "kimi-k2.7-code-highspeed")]

DIFF = """def apply_discount(price, pct):
    if pct > 100:
        pct = 100
    return price - price * pct / 100
"""
PROMPT = ("Review this function and list every correctness issue you find, one per line, "
          "each with the line number. If there are none, say NONE.\n\n" + DIFF)

# THE CAP IS NOT A LATENCY CONTROL. Making a model "fast" by cutting it off at max_tokens is truncation —
# you pay for the input and get a body that does not parse, which is 100% waste, and on a reasoning model it
# is worse: measured earlier, kimi-k3 returned ZERO characters on 19 of 20 calls at max_tokens=2000 because
# reasoning consumed the budget before any answer was emitted. So every condition keeps a GENEROUS measured
# cap. Latency must fall because the model SAYS less, never because it was interrupted — and a truncated
# result here is recorded as a FAILURE of the condition, not a win.
_SCHEMA = {"type": "object",
           "properties": {"issues": {"type": "array",
                                     "items": {"type": "object",
                                               "properties": {"line": {"type": "integer"},
                                                              "issue": {"type": "string"}},
                                               "required": ["line", "issue"]}}},
           "required": ["issues"]}
_TERSE = ("You are a code reviewer. Be terse: no preamble, no restatement of the code, no summary. "
          "One short sentence per issue.")

# Three conditions isolate the MECHANISM. `terse` separates "stop narrating" (an instruction) from
# "answer in this shape" (a contract) — they are different asks and may not cost the same.
CONDITIONS = [
    ("free",  {"system": None,   "schema": None},     "no system, no contract"),
    ("terse", {"system": _TERSE, "schema": None},     "instruction only"),
    ("bound", {"system": _TERSE, "schema": _SCHEMA},  "instruction + output contract"),
]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--repeats", type=int, default=3, help="calls per cell; one call cannot separate fast from lucky")
    ap.add_argument("--budget", type=float, default=3.0, help="HARD stop in $")
    ap.add_argument("--estimate", action="store_true")
    a = ap.parse_args()
    os.environ["SPENDGUARD_ADVISOR_EXECUTOR"] = "api"

    if a.estimate:
        tot, unpriced = 0.0, []
        for v, m in CANDIDATES:
            try:
                c = pricing.realtime_cost(m, len(PROMPT) // 4, 1800)
            except Exception:
                c = None
            if not c:
                unpriced.append(m)
                continue
            tot += c * a.repeats * len(CONDITIONS)
        print(f"ZERO-SPEND ESTIMATE — {len(CANDIDATES) * len(CONDITIONS) * a.repeats} calls, ~${tot:,.3f} "
              f"(hard budget ${a.budget:,.2f})")
        if unpriced:
            # unpriced != free. A model we cannot price still bills; say so rather than quietly excluding it.
            print(f"  {len(unpriced)} model(s) spendguard cannot price — real usage, unknown $, NOT counted "
                  f"as zero: {', '.join(unpriced)}")
            print("  price them first: spendguard price <model> --in <$/1M> --out <$/1M> --source '<url>'")
        return 0

    out_path = os.path.join(str(config.HOME), "latency_levers.jsonl")
    spent, rows = 0.0, []
    print(f"  {'vendor/model':<34}{'cond':<8}{'ok':>5}{'med out':>9}{'med lat':>9}{'tok/s':>8}{'kinds'}")
    for v, m in CANDIDATES:
        served = vc.serves(v, m)
        if served is False:
            print(f"  {v+'/'+m:<34}NOT SERVED — skipping (discovery said so; not a guess)")
            continue
        for label, cond, _why in CONDITIONS:
            outs, lats, kinds = [], [], {}
            for _ in range(a.repeats):
                if spent > a.budget:
                    print(f"  BUDGET STOP at ${spent:,.3f}")
                    break
                sig = vc.class_sig(m, f"levers:{label}")
                budget_s, _b = vc.time_budget(v, m, sig=sig, default_s=300)
                cap, _cb = vc.output_cap(v, m, sig=sig)   # measured/published — generous, never a latency knob
                res = vc.call(v, m, PROMPT, deadline_s=budget_s or 300, purpose=f"levers:{label}",
                              system=cond["system"], schema=cond["schema"], max_tokens=cap)
                spent += res.cost or 0.0
                kinds[res.kind] = kinds.get(res.kind, 0) + 1
                if res.ok and res.out_tok:
                    outs.append(res.out_tok)
                    lats.append(res.latency)
            med_out = int(statistics.median(outs)) if outs else 0
            med_lat = statistics.median(lats) if lats else 0.0
            tps = (statistics.median([o / l for o, l in zip(outs, lats) if l > 0]) if outs else 0.0)
            ks = " ".join(f"{k}:{n}" for k, n in sorted(kinds.items()))
            warn = "  ← TRUNCATED: not a speed win, a destroyed answer" if kinds.get("truncated") else ""
            print(f"  {v+'/'+m:<34}{label:<8}{len(outs):>3}/{a.repeats:<2}{med_out:>9,}{med_lat:>8.1f}s"
                  f"{tps:>8.1f}  {ks}{warn}")
            rows.append({"vendor": v, "model": m, "condition": label, "ok": len(outs), "n": a.repeats,
                         "med_out": med_out, "med_lat": round(med_lat, 1), "tok_s": round(tps, 1),
                         "kinds": kinds})
    with open(out_path, "a") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")

    print("\n  LEVER 1 — constraining the output (SAME model, so no quality is traded away):")
    for v, m in {(r["vendor"], r["model"]) for r in rows}:
        f = next((r for r in rows if r["model"] == m and r["condition"] == "free" and r["ok"]), None)
        for cond in ("terse", "bound"):
            b = next((r for r in rows if r["model"] == m and r["condition"] == cond and r["ok"]), None)
            if f and b and b["med_lat"]:
                print(f"    {m:<26}{cond:<7}{f['med_lat']:>7.1f}s -> {b['med_lat']:>6.1f}s  "
                      f"({f['med_lat'] / b['med_lat']:.1f}x, {f['med_out']:,} -> {b['med_out']:,} tokens)")
    print("\n  LEVER 2 — a lighter sibling. A QUALITY TRADE: fine for classification, wrong for review.")
    for r in sorted([r for r in rows if r["condition"] == "bound" and r["ok"]], key=lambda r: r["med_lat"]):
        print(f"    {r['vendor']+'/'+r['model']:<34}{r['med_lat']:>7.1f}s   {r['tok_s']:>6.1f} tok/s")
    print(f"\nDONE — ${spent:,.3f}. rows -> {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
