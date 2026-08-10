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


def learn_from_wave(rows, wave_no, judge_model="claude-opus-4-8"):
    """AGENTIC: read a wave's findings and say what the next wave should know. Returns (suppress, recur).

    WHAT IS SAFE TO CARRY FORWARD, and what is not.
      SAFE — noise categories. If every vendor keeps reporting something the brief already excluded, saying
      so again removes cost without removing coverage.
      SAFE — recurring defect CLASSES. A bug found in three files is likely in a fourth; a human reviewer
      would carry that forward, and so should this.
      NOT SAFE — anything that narrows the brief. A later wave told only to look for what earlier waves found
      will confirm wave 1 and miss what wave 1 missed, while the finding count goes UP and looks like
      improvement. Learnings are appended to the base instruction, never substituted for it.

    Whether it actually helped is measurable and reported: a finding that only appeared after we asked for it
    is weaker evidence than one found cold, so `prompted` findings are counted separately."""
    if not rows:
        return [], []
    sample = [f"{r['severity']}|{r['issue'][:160]}" for r in rows[:120]]
    prompt = ("Below are code-review findings from one wave of an automated multi-vendor review.\n\n"
              "1. NOISE: which finding categories are NOT real correctness defects (style, naming, "
              "formatting, missing type hints, speculative 'could be improved')? These were already excluded "
              "by the brief and are being reported anyway.\n"
              "2. RECURRING: which specific defect CLASSES appear in more than one file and are likely to "
              "recur elsewhere in this codebase?\n\n"
              "Reply as JSON only: {\"noise\": [\"...\"], \"recurring\": [\"...\"]}. "
              "Be specific and terse. At most 5 of each.\n\nFINDINGS:\n" + "\n".join(sample))
    schema = {"type": "object", "required": ["noise", "recurring"],
              "properties": {"noise": {"type": "array", "items": {"type": "string"}},
                             "recurring": {"type": "array", "items": {"type": "string"}}}}
    v = "anthropic" if judge_model.startswith("claude") else "openai"
    r = vc.call(v, judge_model, prompt, deadline_s=300, purpose=f"review:wave-learn-{wave_no}",
                system="You are a review-quality analyst. Be terse and specific.", schema=schema)
    if not r.ok:
        print(f"    wave-learning FAILED ({r.kind}) — carrying nothing forward rather than guessing")
        return [], []
    try:
        d = json.loads(r._text) if isinstance(r._text, str) else r._text
        noise = [str(x)[:160] for x in (d.get("noise") or [])][:5]
        recur = [str(x)[:160] for x in (d.get("recurring") or [])][:5]
    except Exception:
        return [], []
    try:
        from spendguard import learn
        for lesson in noise:
            learn.add_insight("review:code-defects", f"NOISE: {lesson}", source=f"wave-{wave_no}",
                              confidence=0.6, ctx={"condition": "multi-vendor code review",
                                                   "action": "suppress this category in the brief",
                                                   "mechanism": "already excluded; still reported"})
        for lesson in recur:
            learn.add_insight("review:code-defects", f"RECURRING: {lesson}", source=f"wave-{wave_no}",
                              confidence=0.6, ctx={"condition": "this codebase",
                                                   "action": "check later files for it explicitly",
                                                   "mechanism": "seen in more than one file"})
    except Exception:
        pass
    return noise, recur


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default="src/spendguard", help="directory to review")
    ap.add_argument("--pattern", default="*.py")
    ap.add_argument("--limit", type=int, default=0, help="0 = every matching file")
    ap.add_argument("--only", default="", help="comma-separated filenames — review just these")
    ap.add_argument("--fill", default="",
                    help="findings file from a PARTIAL run: re-ask only the (file, vendor) pairs that had "
                         "no reviewer, using its coverage rows. A vendor that could not answer is not a "
                         "vendor that found nothing, so the gap is re-runnable rather than re-paying for "
                         "the whole panel — measured on wave 2, 13 calls instead of 40.")
    ap.add_argument("--min-chars", type=int, default=1500, help="skip trivial files")
    ap.add_argument("--budget", type=float, default=25.0, help="HARD stop in $")
    ap.add_argument("--tolerance", type=float, default=0.50,
                    help="$ by which the caller's counter may lag the LEDGER before the run stops. The two "
                         "should now agree; a gap means calls are billing that the caller cannot see.")
    ap.add_argument("--estimate", action="store_true")
    ap.add_argument("--out", default="")
    ap.add_argument("--append", action="store_true",
                    help="add to an existing results file instead of replacing it — a second pass over more "
                         "files must not destroy the findings the first pass already paid for")
    ap.add_argument("--waves", default="",
                    help="comma-separated wave sizes, e.g. 10,20,20,30. After each wave an AGENTIC pass "
                         "reads that wave's findings and proposes what the next wave should know. Learnings "
                         "are ADDITIVE to the base instruction, never a replacement — narrowing the brief to "
                         "what we already found would make later waves confirm wave 1 instead of reviewing.")
    ap.add_argument("--files-at-once", type=int, default=3,
                    help="files reviewed concurrently. fan_out already parallelises the four VENDORS within a "
                         "file, but every file still waited for the slowest of them: 10 files took 49 minutes, "
                         "so 90 would take 7 hours. Overlapping files hides that tail. Each vendor sees this "
                         "many concurrent calls, so keep it modest.")
    a = ap.parse_args()
    os.environ["SPENDGUARD_ADVISOR_EXECUTOR"] = "api"      # the API path: a lane reports session accounting

    ts = targets(a.root, a.pattern, a.limit, a.min_chars)
    if a.only:
        want = {x.strip() for x in a.only.split(",")}
        ts = [t for t in ts if t["name"] in want]
    # WHICH VENDORS EACH FILE STILL NEEDS. Defaults to the whole panel; --fill narrows it per file to the
    # ones that could not answer last time, which is exactly what the coverage rows were written to enable.
    panel_for = {t["name"]: list(PANEL) for t in ts}
    prior_cov = {}
    if a.fill:
        cov = prior_cov
        for line in open(a.fill):
            if not line.strip():
                continue
            r = json.loads(line)
            if r.get("kind") == "coverage":
                cov[pathlib.Path(r["file"]).name] = r
        kept = []
        for t in ts:
            c = cov.get(t["name"])
            if c is None:
                kept.append(t)                     # never reviewed at all — the full panel
                continue
            missing = set((c.get("unavailable") or {}).keys())
            need = [(v, m) for (v, m) in PANEL if v in missing]
            if need:
                panel_for[t["name"]] = need
                kept.append(t)
        skipped = len(ts) - len(kept)
        ts = kept
        n_calls = sum(len(panel_for[t["name"]]) for t in ts)
        print(f"FILL MODE — {n_calls} gap call(s) across {len(ts)} file(s); "
              f"{skipped} file(s) already had the full panel and are NOT re-paid for.")
        for t in ts:
            print(f"    {t['name']:<24} needs {','.join(v for v, _ in panel_for[t['name']])}")
    if not ts:
        print(f"no files matching {a.pattern} under {a.root}"
              + (" (or no gaps to fill)" if a.fill else ""))
        return 1
    chars = sum(t["chars"] for t in ts)
    print(f"{len(ts)} files, {chars:,} chars (~{chars // 4:,} input tokens) from {a.root}")

    if a.estimate:
        from spendguard import expected_output
        # THE ESTIMATE COUNTS WHAT WILL ACTUALLY RUN. In fill mode most (file, vendor) pairs are skipped,
        # and quoting the full panel would overstate the cost by 3x here — an estimate that does not match
        # the run is the wrong-number problem this project exists to fix, committed in the estimator.
        n_calls = sum(len(panel_for[t["name"]]) for t in ts)
        print(f"\nZERO-SPEND ESTIMATE — {n_calls} call(s) across {len(ts)} file(s)"
              + (" (gap fill)" if a.fill else f" x {len(PANEL)} vendors"))
        tot = 0.0
        for v, m in PANEL:
            pred, basis = expected_output.expect(m)
            sub = sum(pricing.realtime_cost(m, t["chars"] // 4, pred or 800) or 0
                      for t in ts if (v, m) in panel_for[t["name"]])
            tot += sub
            print(f"  {v:<10} {m:<20} ${sub:>8,.2f}   (output {pred:,} tok/file, basis {basis})")
        print(f"  {'TOTAL':<32} ${tot:>8,.2f}   (hard budget ${a.budget:,.2f})")
        return 0

    ap_tol = a.tolerance
    out_path = a.out or os.path.join(str(config.HOME), "repo_review.jsonl")
    spent, started = 0.0, time.time()

    def ledger_spent_since(t0):
        """What the LEDGER says this run cost. The script's own counter is the thing under test, so it cannot
        also be the thing that verifies it — an independent record is the only reason the previous run's
        overspend was ever noticed."""
        import sqlite3
        try:
            c = sqlite3.connect(config.db_path())
            r = c.execute("SELECT COALESCE(SUM(cost),0) FROM calls WHERE ts >= ?", (t0,)).fetchone()
            return float(r[0] or 0)
        except Exception:
            return -1.0

    import datetime
    t0_iso = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
    fh = open(out_path, "a" if a.append else "w")   # INCREMENTAL: a killed run keeps what it already paid for
    per_vendor = collections.defaultdict(lambda: {"files": 0, "ok": 0, "findings": 0, "kinds": {}})
    rows = []
    print(f"\nreviewing — hard budget ${a.budget:,.2f}, results -> {out_path}\n")
    waves = [int(x) for x in a.waves.split(",") if x.strip().isdigit()] if a.waves else [len(ts)]
    learned_noise, learned_recur = [], []
    wave_stats = []

    def brief():
        """Base instruction ALWAYS first; learnings appended. Never a substitute — see learn_from_wave."""
        b = SYSTEM
        if learned_noise:
            b += ("\n\nAlready-known NOISE in this codebase — do not report these:\n"
                  + "\n".join(f"- {x}" for x in learned_noise))
        if learned_recur:
            b += ("\n\nDefect classes seen in earlier files here — check for them IN ADDITION to everything "
                  "else, never instead of it:\n" + "\n".join(f"- {x}" for x in learned_recur))
        return b

    import threading
    import concurrent.futures as _cf
    lock = threading.Lock()
    stop = threading.Event()
    done = {"n": 0}

    def review_one(idx, t):
        """One file, all four vendors. Runs on a worker so the slowest vendor of file A overlaps file B."""
        nonlocal spent
        if stop.is_set():
            return None
        fan = vc.fan_out(panel_for[t["name"]], prompt_for(t), deadline_s=300, purpose=f"review:{t['name']}",
                         system=brief(), schema=FINDING_SCHEMA)
        local = []
        with lock:
            for r in fan["results"]:
                st = per_vendor[r.vendor]
                st["files"] += 1
                st["kinds"][r.kind] = st["kinds"].get(r.kind, 0) + 1
                spent += r.cost or 0.0
                if not r.ok:
                    continue
                ok_shape, _salv, _why = output_contract.check_item(r._text, FINDING_SCHEMA)
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
                    row = {"file": t["path"], "vendor": r.vendor, "line": it.get("line"),
                           "severity": str(it.get("severity", ""))[:24],
                           "issue": str(it.get("issue", ""))[:400]}
                    local.append(row)
                    rows.append(row)
                    fh.write(json.dumps(row) + "\n")
            # PER-FILE COVERAGE, WRITTEN WITH THE FINDINGS. A vendor that could not review a file is not a
            # vendor that found nothing in it, and until now only the console knew the difference: the
            # findings file recorded issues but never who was ASKED, so downstream "2+ vendors agree"
            # counted agreement out of an unknown denominator. On the largest files that denominator really
            # does shrink — Moonshot refuses source payloads over roughly 17,000 chars ("considered high
            # risk"), so a 53,000-char module is reviewed by three vendors while the gate assumes four.
            # MERGED WITH WHAT WAS ALREADY KNOWN. In fill mode this run asks only the vendors that were
            # missing, so a coverage row written from THIS fan-out alone would say "reviewers: [zai]" and
            # silently drop the two that succeeded last time — turning a 4-vendor file back into a
            # 1-vendor one in the record. The same absence-read-as-an-answer bug this row exists to expose,
            # committed by the thing exposing it.
            prior = prior_cov.get(t["name"]) or {}
            reviewed = set(prior.get("reviewers") or []) | {r.vendor for r in fan["results"] if r.ok}
            unavail = {k: v for k, v in (prior.get("unavailable") or {}).items() if k not in reviewed}
            unavail.update({r.vendor: r.kind for r in fan["results"] if not r.ok})
            fh.write(json.dumps({
                "kind": "coverage", "file": t["path"], "chars": t.get("chars"),
                "reviewers": sorted(reviewed),
                "unavailable": {k: v for k, v in unavail.items() if k not in reviewed},
            }) + "\n")
            fh.flush()
            done["n"] += 1
            led = ledger_spent_since(t0_iso)
            drift = (led - spent) if led >= 0 else 0.0
            over = spent > a.budget
            # The accounting check, every file: the previous run reported $1.98 while the ledger said $13.12.
            bad_acct = led >= 0 and spent > 0.05 and drift > max(ap_tol, spent * 0.25)
            got = ",".join(f"{r.vendor[:4]}={'ok' if r.ok else r.kind[:4]}" for r in fan["results"])
            # THE DENOMINATOR IS WHAT WAS ASKED, NOT THE PANEL SIZE. In fill mode one vendor is asked and
            # "1/4" reads as three failures — a wrong denominator making a clean run look broken, which is
            # the same defect the coverage rows were added to fix, printed one line above them.
            print(f"  [{done['n']:>3}/{len(ts)}] {t['name'][:28]:<30}"
                  f"{fan['n_ok']}/{len(panel_for[t['name']])}  {got}  "
                  f"${spent:,.2f} (ledger ${led:,.2f})"
                  + ("  <-- LEDGER DISAGREES" if bad_acct else "")
                  + ("  <-- OVER BUDGET" if over else ""), flush=True)
            if bad_acct:
                print(f"\n  STOPPING: counter and ledger differ by ${drift:,.2f} — a hard stop that cannot "
                      f"see the money is not a hard stop.")
                stop.set()
            elif over:
                print(f"\n  BUDGET STOP at ${spent:,.2f} of ${a.budget:,.2f}.")
                stop.set()
        return local

    cursor = 0
    for wi, size in enumerate(waves, 1):
        batch = ts[cursor:cursor + size]
        cursor += size
        if not batch or stop.is_set():
            break
        before_rows, before_spent = len(rows), spent
        print(f"\n  ── WAVE {wi}: {len(batch)} files"
              + (f", carrying {len(learned_noise)} noise + {len(learned_recur)} recurring lessons forward"
                 if (learned_noise or learned_recur) else ", no prior lessons") + " ──", flush=True)
        with _cf.ThreadPoolExecutor(max_workers=max(1, a.files_at_once)) as pool:
            futs = [pool.submit(review_one, i, t) for i, t in enumerate(batch, 1)]
            for f in _cf.as_completed(futs):
                try:
                    f.result()
                except Exception as e:
                    print(f"  file worker died: {type(e).__name__}: {e}", flush=True)
        wrows = rows[before_rows:]
        wcost = spent - before_spent
        sites = {}
        for r in wrows:
            sites.setdefault((r["file"], r["line"]), set()).add(r["vendor"])
        multi = sum(1 for v in sites.values() if len(v) > 1)
        wave_stats.append({"wave": wi, "files": len(batch), "findings": len(wrows), "sites": len(sites),
                           "multi_vendor": multi, "cost": round(wcost, 2)})
        print(f"    wave {wi}: {len(wrows)} findings across {len(sites)} sites, {multi} multi-vendor, "
              f"${wcost:,.2f}  (${wcost/max(len(batch),1):.3f}/file)", flush=True)
        if cursor < len(ts) and not stop.is_set():
            n, rc = learn_from_wave(wrows, wi)
            for x in n:
                if x not in learned_noise:
                    learned_noise.append(x)
            for x in rc:
                if x not in learned_recur:
                    learned_recur.append(x)
            if n or rc:
                print(f"    carried forward -> {len(n)} noise, {len(rc)} recurring", flush=True)
                for x in (n + rc)[:4]:
                    print(f"      · {x[:96]}", flush=True)
    fh.close()

    if len(wave_stats) > 1:
        print("\n  WAVE OVER WAVE — is it getting better, or just being told what to find?")
        print(f"  {'wave':<6}{'files':>6}{'findings':>10}{'multi-vendor':>14}{'$/file':>9}{'multi %':>9}")
        for w in wave_stats:
            pct = (w["multi_vendor"] / w["sites"] * 100) if w["sites"] else 0
            print(f"  {w['wave']:<6}{w['files']:>6}{w['findings']:>10}{w['multi_vendor']:>14}"
                  f"{w['cost']/max(w['files'],1):>9.3f}{pct:>8.0f}%")
        print("  multi-vendor % is the honest signal: findings a SINGLE vendor reports after we told it what "
              "to look for are the ones most likely to be echo.")

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
