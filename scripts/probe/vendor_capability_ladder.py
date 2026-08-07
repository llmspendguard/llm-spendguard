"""THE harness. Every vendor, every rung from trivial to a real code review, against a declared PASS CONTRACT.

WHAT THIS REPLACES. Five probes were written in one day, each answering one question, and together they still
could not answer "is this vendor fit for this kind of work". This is the consolidation:

    panel_reliability_check.py   -> rung `review`, with rounds. Subsumed.
    latency_levers_ab.py         -> the `terse` system prompt is now standard on every rung. Subsumed.
    streaming_transport_probe.py -> answered (always stream anthropic); kept only as history.
    estimator_calibration_run.py -> a different question (cost estimation on synthetic shapes). Kept.
    realprompt_replay.py         -> a different question (replay real recorded work). Kept.

THE IDEA: A RUNG IS A CONTRACT, NOT A PROMPT.
Every rung declares the SHAPE its answer must arrive in, checked by output_contract against the real bytes.
"HTTP 200" is not a pass and never was — the failure this exists to catch is the one that looks like success:
an empty body, a truncated JSON object that reads as "no findings", a prose preamble wrapped around the
answer, a required field present but returned as 0. Each of those has produced a confident wrong number here
already.

THE LADDER, cheapest first, so a vendor that cannot do the simple thing is found before it is paid to attempt
the complex one:

    smoke     can it answer at all                       contract: the literal token comes back
    enum      a forced binary judgement                  contract: the answer IS one of the two words
    json      a small object with required fields        contract: keys present AND non-empty
    review    multi-finding code review, free-form       contract: JSON object, issues array, real line numbers
    bigin     the same review over a LARGE real input    contract: as review — tests context handling
    strict    the same review under a FORCED schema      contract: as review — tests the provider's schema path

WHY REPEATS. Reliability is a RATE. One green call cannot distinguish a vendor that always works from one
that works four times in five, and the second is what ruins a batch at item 400.

WHY NO max_tokens ANYWHERE. A cap is a termination bound sized from measurement; passing a literal here is
the mistake that produced 19-of-20 empty responses. Omitted, so the measured bound is used. Same for the
deadline: each vendor gets its own, from its own recorded p99.

READ A FAILURE BY ITS KIND — the remedies are different and must never be merged into one "error":
    transport_error   transient; retried inside call()
    deadline_exceeded the BUDGET was wrong, or the vendor is down
    empty / truncated the CAP was wrong
    schema_violation  the provider's structured-output path does not do what it claims
    contract-fail     it answered, in the wrong shape — the one a status code cannot see
"""
import argparse, json, os, pathlib, statistics, sys, time

import spendguard                                   # noqa: F401
spendguard.require()
from spendguard import config, output_contract, pricing, vendor_call as vc   # noqa: E402

PANEL = [("anthropic", "claude-opus-4-8"), ("openai", "gpt-5.5"),
         ("moonshot", "kimi-k3"), ("zai", "glm-5.2")]

# Measured this session: a terse instruction alone cut kimi-k3 from 28.4s to 14.9s and glm-5.2 from 25.9s to
# 18.8s, with no model change and therefore no quality traded. It is standard on every rung, not an option.
TERSE = ("Be terse. No preamble, no restatement of the input, no summary. Answer only what was asked.")


def _repo_source(target_chars):
    """A LARGE input that is real code, read at runtime — never a pasted blob that rots in this file.

    Concatenates this package's own modules until the target size is reached, so `bigin` reviews something
    that actually exists and the rung stays honest as the codebase changes."""
    root = pathlib.Path(__file__).resolve().parents[2] / "src" / "spendguard"
    buf = []
    total = 0
    for f in sorted(root.glob("*.py")):
        try:
            t = f.read_text()
        except Exception:
            continue
        buf.append(f"# ---- {f.name} ----\n{t}")
        total += len(t)
        if total >= target_chars:
            break
    return "\n".join(buf)[:target_chars]


REVIEW_DIFF = """def apply_discount(price, pct):
    if pct > 100:
        pct = 100
    return price - price * pct / 100
"""

REVIEW_SCHEMA = {"type": "object",
                 "required": ["issues"],
                 "properties": {"issues": {"type": "array",
                                           "items": {"type": "object",
                                                     "required": ["line", "issue"],
                                                     "nonempty": ["issue"],
                                                     "properties": {"line": {"type": "integer"},
                                                                    "issue": {"type": "string"}}}}}}


def _is_yes_no(item):
    """The answer must BE the judgement, not contain it. `check_item` calls this once with the raw text."""
    return isinstance(item, str) and item.strip().strip('".').upper() in ("YES", "NO")


def _says_ok(item):
    return isinstance(item, str) and "OK" in item.strip().upper()[:40]


def rungs(big_chars):
    """The ladder, as data. Each rung: id, prompt, system, schema (forced or None), contract, why it exists."""
    return [
        {"id": "smoke", "why": "can it answer at all",
         "prompt": "Reply with exactly: OK", "system": TERSE, "schema": None, "contract": _says_ok},
        {"id": "enum", "why": "a forced binary judgement — the shape that answered in 4 tokens historically",
         "prompt": 'Is "hydrogen 6 %" a valid alternative name for the concept "exposure to topical '
                   'antiseptics"? Answer with exactly YES or NO and nothing else.',
         "system": TERSE, "schema": None, "contract": _is_yes_no},
        {"id": "json", "why": "a small object with required, NON-EMPTY fields",
         "prompt": 'Base-concept: "ranitidine effervescent". Identify the broader canonical concept.\n'
                   'Reply as JSON only: {"parent": "<string>", "confidence": "high|medium|low"}',
         "system": TERSE, "schema": None,
         "contract": {"type": "object", "required": ["parent", "confidence"],
                      "nonempty": ["parent", "confidence"]}},
        # THE CONTROL. Deliberately asks for JSON in the PROMPT rather than forcing it, because the
        # comparison against `strict` is the finding: measured, prompt-asked JSON passed on 1 of 4 vendors
        # while provider-forced JSON passed on 4 of 4. Every failure here was "salvaged" — the answer was
        # right and arrived wrapped in a code fence, which a downstream parser may or may not survive.
        # Keep this rung red rather than deleting it; it is the evidence for always passing schema=.
        {"id": "review", "why": "CONTROL: JSON asked for in the prompt, not enforced — expect fences",
         "prompt": "Review this function. Reply as JSON only: "
                   '{"issues": [{"line": <int>, "issue": "<short sentence>"}]}\n\n' + REVIEW_DIFF,
         "system": TERSE, "schema": None, "contract": REVIEW_SCHEMA},
        # Forced schema, so this rung isolates CONTEXT HANDLING. With JSON merely asked for, a failure here
        # is ambiguous — did the model mishandle 10K tokens, or just add a fence? Now only the first can fail.
        {"id": "bigin", "why": "the same work over a LARGE real input — context handling, schema FORCED",
         "prompt": "Review the LAST function in this source dump only, ignoring everything above it.\n\n"
                   + _repo_source(big_chars) + "\n\n" + REVIEW_DIFF,
         "system": TERSE, "schema": REVIEW_SCHEMA, "contract": REVIEW_SCHEMA},
        {"id": "strict", "why": "the provider's FORCED-schema path — known red on some vendors",
         "prompt": "Review this function and list every correctness issue.\n\n" + REVIEW_DIFF,
         "system": TERSE, "schema": REVIEW_SCHEMA, "contract": REVIEW_SCHEMA},
    ]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--repeats", type=int, default=3,
                    help="calls per (rung, vendor). Reliability is a RATE; one green call proves nothing.")
    ap.add_argument("--rungs", default="", help="comma-separated rung ids; default = the whole ladder")
    ap.add_argument("--big-chars", type=int, default=40000, help="input size for the `bigin` rung")
    ap.add_argument("--pass-rate", type=float, default=1.0,
                    help="fraction of repeats that must satisfy the CONTRACT for a rung to pass on a vendor. "
                         "Default 1.0: for a batch of 400 this is the rate that matters, and anything less "
                         "means picking which failures to tolerate — a decision that belongs to you, out loud, "
                         "not to a default.")
    ap.add_argument("--budget", type=float, default=6.0, help="HARD stop in $")
    ap.add_argument("--estimate", action="store_true")
    ap.add_argument("--compare", action="store_true", help="diff against the previous run; zero spend")
    a = ap.parse_args()
    os.environ["SPENDGUARD_ADVISOR_EXECUTOR"] = "api"     # the API path: a lane reports session accounting

    out_path = os.path.join(str(config.HOME), "capability_ladder.jsonl")
    ladder = rungs(a.big_chars)
    if a.rungs:
        want = {x.strip() for x in a.rungs.split(",")}
        ladder = [r for r in ladder if r["id"] in want]

    if a.compare:
        return compare(out_path)

    if a.estimate:
        tot = 0.0
        print(f"{'rung':<10}{'input tok':>11}   why")
        for r in ladder:
            it = len(r["prompt"]) // 4
            print(f"  {r['id']:<8}{it:>11,}   {r['why']}")
            for _v, m in PANEL:
                # Estimate output the way the gate would, not with a private guess.
                from spendguard import expected_output
                pred, _b = expected_output.expect(m)
                try:
                    tot += (pricing.realtime_cost(m, it, pred or 800) or 0) * a.repeats
                except Exception:
                    pass
        print(f"\nZERO-SPEND ESTIMATE — {len(ladder) * len(PANEL) * a.repeats} calls, ~${tot:,.3f} "
              f"(hard budget ${a.budget:,.2f})")
        return 0

    spent, rows = 0.0, []
    started = time.time()
    print(f"  {'rung':<9}{'vendor':<11}{'answered':>9}{'contract':>10}{'p50 lat':>9}{'med out':>9}  kinds / first failure")
    for r in ladder:
        for v, m in PANEL:
            oks, conform, lats, outs, kinds, why = 0, 0, [], [], {}, ""
            for _ in range(a.repeats):
                if spent > a.budget:
                    print(f"  BUDGET STOP at ${spent:,.3f}")
                    break
                sig = vc.class_sig(m, f"ladder:{r['id']}")
                budget_s, _b = vc.time_budget(v, m, sig=sig, default_s=300)
                # NO max_tokens. The measured bound is used; a literal here is the recurring mistake.
                res = vc.call(v, m, r["prompt"], deadline_s=budget_s or 300, purpose=f"ladder:{r['id']}",
                              system=r["system"], schema=r["schema"])
                spent += res.cost or 0.0
                kinds[res.kind] = kinds.get(res.kind, 0) + 1
                if not res.ok:
                    continue
                oks += 1
                lats.append(res.latency)
                outs.append(res.out_tok)
                # THE PART A STATUS CODE CANNOT SEE: did the answer arrive in the declared SHAPE?
                ok_shape, salvaged, reason = output_contract.check_item(res._text, r["contract"])
                if ok_shape and not salvaged:
                    conform += 1
                elif not why:
                    why = reason or ("salvaged: needed a fence/preamble stripped — a downstream parser may not")
            n = sum(kinds.values()) or 1
            rate = conform / float(n)
            ks = " ".join(f"{k}:{c}" for k, c in sorted(kinds.items()) if k != "ok")
            mark = "PASS" if rate >= a.pass_rate else "FAIL"
            print(f"  {r['id']:<9}{v:<11}{oks:>4}/{n:<4}{conform:>6}/{n:<3}"
                  f"{(statistics.median(lats) if lats else 0):>8.1f}s"
                  f"{(int(statistics.median(outs)) if outs else 0):>9,}  {mark} {ks} {why[:52]}", flush=True)
            rows.append({"ts": time.time(), "rung": r["id"], "vendor": v, "model": m, "n": n, "ok": oks,
                         "conform": conform, "rate": round(rate, 3), "pass": rate >= a.pass_rate,
                         "kinds": kinds, "first_failure": why[:200],
                         "lat_p50": round(statistics.median(lats), 1) if lats else None,
                         "med_out": int(statistics.median(outs)) if outs else 0})
    with open(out_path, "a") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")

    print(f"\n  VERDICT — a vendor is fit for a rung only if it met the contract {a.pass_rate:.0%} of the time")
    print(f"  {'vendor':<11}" + "".join(f"{r['id']:>9}" for r in ladder))
    for v, _m in PANEL:
        cells = []
        for r in ladder:
            row = next((x for x in rows if x["rung"] == r["id"] and x["vendor"] == v), None)
            cells.append("—" if not row else ("PASS" if row["pass"] else f"{row['conform']}/{row['n']}"))
        print(f"  {v:<11}" + "".join(f"{c:>9}" for c in cells))
    bad = [x for x in rows if not x["pass"]]
    print(f"\n  {len(rows) - len(bad)}/{len(rows)} cells fit. "
          + ("ALL VENDORS FIT FOR ALL RUNGS." if not bad else "NOT FIT: "
             + ", ".join(f"{x['vendor']}/{x['rung']}" for x in bad)))
    print(f"  ${spent:,.3f} in {time.time() - started:.0f}s  ->  {out_path}")
    print("  `--compare` diffs the next run against this one; a rung that PASSED and now fails is a regression.")
    return 0


def compare(path):
    """Diff the last two runs. A vendor that met a contract yesterday and does not today is a REGRESSION —
    and without this the ladder is a snapshot, which cannot tell you whether anything changed."""
    if not os.path.exists(path):
        print(f"no history at {path}")
        return 1
    rows = [json.loads(x) for x in open(path) if x.strip()]
    if not rows:
        print("no rows")
        return 1
    latest = max(r["ts"] for r in rows)
    cur = {(r["rung"], r["vendor"]): r for r in rows if r["ts"] >= latest - 3600}
    prev_rows = [r for r in rows if r["ts"] < latest - 3600]
    if not prev_rows:
        print(f"only one run recorded ({len(cur)} cells) — nothing to compare against yet")
        return 0
    prev_ts = max(r["ts"] for r in prev_rows)
    prev = {(r["rung"], r["vendor"]): r for r in prev_rows if r["ts"] >= prev_ts - 3600}
    print(f"  {'cell':<26}{'was':>10}{'now':>10}   verdict")
    regressions = 0
    for k in sorted(set(cur) | set(prev)):
        c, p = cur.get(k), prev.get(k)
        cw = f"{c['conform']}/{c['n']}" if c else "—"
        pw = f"{p['conform']}/{p['n']}" if p else "—"
        verdict = ""
        if c and p and p["pass"] and not c["pass"]:
            verdict = "REGRESSION"
            regressions += 1
        elif c and p and not p["pass"] and c["pass"]:
            verdict = "fixed"
        if verdict:
            print(f"  {k[1] + '/' + k[0]:<26}{pw:>10}{cw:>10}   {verdict}")
    print(f"\n  {regressions} regression(s)")
    return 1 if regressions else 0


if __name__ == "__main__":
    sys.exit(main())
