"""Review a repo with all four vendors at once, and report only what survives being checked.

WHY A PANEL AND NOT ONE REVIEWER. Four models disagree, and the disagreement is the signal: a defect all four
independently name is worth reading first, one that a single vendor reports is worth reading sceptically. That
ordering is free — it falls out of asking four instead of one — and it is the only cheap defence against a
confident wrong finding, which is the failure mode that has cost the most time in this project.

EVERYTHING HERE IS A RAIL BUILT AND MEASURED TODAY, NOT A GUESS:
  * per-file units, so a finding's line number points at a real line and the reviewer sees a whole module
  * FORCED schema (schema=), because JSON asked for in the prompt passed 1 of 4 vendors and enforced by the
    provider passed 4 of 4
  * a terse system prompt, which alone cut kimi-k3 from 28.4s to 14.9s with no model change
  * NO max_tokens and NO deadline literal — both come from measurement, and passing either as a number is the
    mistake that produced 19-of-20 empty responses and a whole invalidated experiment
  * source_compact to drop docstrings/comments: 28% fewer input tokens for identical code
  * output_contract on every response: "HTTP 200" was never a pass

COVERAGE IS REPORTED, NEVER ASSUMED. A vendor that refuses or times out on a file is recorded per file, and
the summary states how many files each vendor actually reviewed. A panel that silently reviewed 61 of 91
files and reported "4-vendor review" would be the exact failure this project keeps finding elsewhere.
Moonshot is expected to lose files here specifically: its content filter fires on this codebase (a
secrets-scanning, PII-handling, key-managing repo) far more than on neutral code.
"""
import argparse, collections, json, os, pathlib, sys, time

import spendguard                                   # noqa: F401
spendguard.require()
from spendguard import config, output_contract, pricing, vendor_call as vc   # noqa: E402
from spendguard.source_compact import compact                                # noqa: E402

PANEL = [("anthropic", "claude-opus-4-8"), ("openai", "gpt-5.5"),
         ("moonshot", "kimi-k3"), ("zai", "glm-5.2")]

SYSTEM = ("You are a meticulous code reviewer. Be terse: no preamble, no restatement of the code, no summary. "
          "Report only DEFECTS you can point at a specific line for — correctness bugs, resource leaks, "
          "unhandled failure modes, security issues, silent-wrong-answer risks. Do not report style, naming, "
          "formatting, or missing type hints. If the file has no real defects, return an empty issues list.")

FINDING_SCHEMA = {
    "type": "object", "required": ["issues"],
    "properties": {"issues": {"type": "array", "items": {
        "type": "object", "required": ["line", "severity", "issue"],
        "nonempty": ["issue", "severity"],       # a required field returned as "" is absence, not an answer
        "properties": {"line": {"type": "integer"},
                       "severity": {"type": "string"},
                       "issue": {"type": "string"}}}}}}


def targets(root, pattern, limit, min_chars):
    out = []
    for f in sorted(pathlib.Path(root).rglob(pattern)):
        if "node_modules" in str(f) or "__pycache__" in str(f):
            continue
        try:
            src = f.read_text(errors="ignore")
        except Exception:
            continue
        if len(src) < min_chars:
            continue
        body, st = compact(src) if f.suffix == ".py" else (src, {"ok": False})
        out.append({"path": str(f), "name": f.name, "src": body, "chars": len(body),
                    "compacted": bool(st.get("ok"))})
    return out[:limit] if limit else out


def prompt_for(t):
    return (f"Review this file for defects: {t['name']}\n\n{t['src']}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default="src/spendguard", help="directory to review")
    ap.add_argument("--pattern", default="*.py")
    ap.add_argument("--limit", type=int, default=0, help="0 = every matching file")
    ap.add_argument("--only", default="", help="comma-separated filenames — review just these")
    ap.add_argument("--min-chars", type=int, default=1500, help="skip trivial files")
    ap.add_argument("--budget", type=float, default=25.0, help="HARD stop in $")
    ap.add_argument("--estimate", action="store_true")
    ap.add_argument("--out", default="")
    a = ap.parse_args()
    os.environ["SPENDGUARD_ADVISOR_EXECUTOR"] = "api"      # the API path: a lane reports session accounting

    ts = targets(a.root, a.pattern, a.limit, a.min_chars)
    if a.only:
        want = {x.strip() for x in a.only.split(",")}
        ts = [t for t in ts if t["name"] in want]
    if not ts:
        print(f"no files matching {a.pattern} under {a.root}")
        return 1
    chars = sum(t["chars"] for t in ts)
    print(f"{len(ts)} files, {chars:,} chars (~{chars // 4:,} input tokens) from {a.root}")

    if a.estimate:
        from spendguard import expected_output
        print(f"\nZERO-SPEND ESTIMATE — {len(ts)} files x {len(PANEL)} vendors = {len(ts) * len(PANEL)} calls")
        tot = 0.0
        for v, m in PANEL:
            pred, basis = expected_output.expect(m)
            sub = sum(pricing.realtime_cost(m, t["chars"] // 4, pred or 800) or 0 for t in ts)
            tot += sub
            print(f"  {v:<10} {m:<20} ${sub:>8,.2f}   (output {pred:,} tok/file, basis {basis})")
        print(f"  {'TOTAL':<32} ${tot:>8,.2f}   (hard budget ${a.budget:,.2f})")
        return 0

    out_path = a.out or os.path.join(str(config.HOME), "repo_review.jsonl")
    spent, started = 0.0, time.time()
    per_vendor = collections.defaultdict(lambda: {"files": 0, "ok": 0, "findings": 0, "kinds": {}})
    rows = []
    print(f"\nreviewing — hard budget ${a.budget:,.2f}, results -> {out_path}\n")
    for i, t in enumerate(ts, 1):
        if spent > a.budget:
            print(f"  BUDGET STOP at ${spent:,.3f} after {i - 1}/{len(ts)} files")
            break
        # fan_out is concurrent and gives each vendor its OWN measured deadline. No max_tokens: the measured
        # cap is used. Both of those are load-bearing, not stylistic.
        fan = vc.fan_out(PANEL, prompt_for(t), deadline_s=300, purpose=f"review:{t['name']}",
                         system=SYSTEM, schema=FINDING_SCHEMA)
        for r in fan["results"]:
            st = per_vendor[r.vendor]
            st["files"] += 1
            st["kinds"][r.kind] = st["kinds"].get(r.kind, 0) + 1
            spent += r.cost or 0.0
            if not r.ok:
                continue
            ok_shape, salvaged, why = output_contract.check_item(r._text, FINDING_SCHEMA)
            if not ok_shape:
                st["kinds"]["contract-fail"] = st["kinds"].get("contract-fail", 0) + 1
                continue
            st["ok"] += 1
            try:
                issues = (json.loads(r._text) if isinstance(r._text, str) else r._text).get("issues") or []
            except Exception:
                issues = []
            st["findings"] += len(issues)
            for it in issues:
                rows.append({"file": t["path"], "vendor": r.vendor, "line": it.get("line"),
                             "severity": str(it.get("severity", ""))[:24], "issue": str(it.get("issue", ""))[:400]})
        got = ",".join(f"{r.vendor[:4]}={'ok' if r.ok else r.kind[:4]}" for r in fan["results"])
        print(f"  [{i:>3}/{len(ts)}] {t['name'][:30]:<32}{fan['n_ok']}/4  {got}  ${spent:,.2f}", flush=True)

    with open(out_path, "w") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")

    print("\n  COVERAGE — how many files each vendor actually reviewed, not how many we asked it to")
    print(f"  {'vendor':<11}{'reviewed':>10}{'findings':>10}   not-ok")
    for v, _m in PANEL:
        st = per_vendor[v]
        bad = " ".join(f"{k}:{n}" for k, n in sorted(st["kinds"].items()) if k != "ok")
        print(f"  {v:<11}{st['ok']:>4}/{st['files']:<5}{st['findings']:>10}   {bad or '—'}")

    # AGREEMENT is the ranking. Same file + same line = the same defect; how many INDEPENDENT vendors named
    # it is the only cheap evidence we have that it is real rather than a confident invention.
    by_site = collections.defaultdict(list)
    for r in rows:
        by_site[(r["file"], r["line"])].append(r)
    ranked = sorted(by_site.items(), key=lambda kv: -len({x["vendor"] for x in kv[1]}))
    agree = collections.Counter(len({x["vendor"] for x in v}) for v in by_site.values())
    print(f"\n  {sum(agree.values())} distinct sites: " +
          " · ".join(f"{n} vendor(s): {agree[n]}" for n in sorted(agree, reverse=True)))
    print("\n  TOP SITES BY AGREEMENT")
    for (path, line), items in ranked[:12]:
        vs = sorted({x["vendor"][:4] for x in items})
        print(f"  {len(vs)}x [{','.join(vs):<22}] {pathlib.Path(path).name}:{line}  {items[0]['issue'][:78]}")
    print(f"\n  ${spent:,.2f} in {time.time() - started:.0f}s   {len(rows)} raw findings -> {out_path}")
    print("  A 1-vendor finding is a LEAD, not a defect: re-read it before acting.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
