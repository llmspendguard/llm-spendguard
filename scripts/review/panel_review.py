"""A 4-LLM review of THIS repo via `spendguard.ask` — three axes + adversarial verify, budget-bounded.

Dogfoods the cross-LLM surface at scale and reviews the client with the full panel (opus/gpt/kimi/glm). It
follows CLAUDE.md #0b — a review only finds defects that FIT the unit it looks at, so it looks along three:

  FILE   (axis 1)  each source file reviewed by the whole panel, in waves — per-file defects.
  SEAM   (axes 2-3) files CLUSTERED agentically (writers+readers of one resource; duplicate concepts) and
                    reviewed together — contract gaps and drift a per-file view cannot see.
  REPO   (axis 4)  the file MAP handed to the panel with one question about a discipline that should hold
                   everywhere — ABSENCE, the only axis that finds what is in NO file.

Then VERIFY: every finding is adversarially checked (refute-by-default) before it is reported, so the output is
real defects, not noise. Honest by construction — spendguard.ask never reads a failed vendor as an answer.

Only moonshot/kimi is metered; opus/gpt/glm ride the $0 lanes. Estimate-first + a hard --budget:

  .venv.nosync/bin/python scripts/review/panel_review.py --estimate            # zero spend: count + $
  .venv.nosync/bin/python scripts/review/panel_review.py --phase all --budget 12
  .venv.nosync/bin/python scripts/review/panel_review.py --phase file --wave 10 --budget 6
"""
import argparse
import concurrent.futures as cf
import json
import os
import pathlib
import time

import spendguard
spendguard.require()
os.environ.setdefault("SPENDGUARD_ADVISOR_EXECUTOR", "pool")   # opus/gpt/glm on the $0 lanes; only kimi bills
from spendguard import pricing, config

REPO = pathlib.Path(__file__).resolve().parents[2]
SRC = REPO / "src" / "spendguard"
OUT = pathlib.Path(config.HOME) / "panel_review_findings.jsonl"     # final: confirmed defects
RAW = pathlib.Path(config.HOME) / "panel_review_raw.jsonl"         # every raw finding, appended AS PRODUCED
DONE = pathlib.Path(config.HOME) / "panel_review_done.txt"         # units reviewed (file names + phase sentinels)


# RESUMABLE — this runs locally, so a closed laptop can pause or kill it. Findings are appended the moment a
# unit finishes, and each finished unit is recorded; a re-launch skips what's done and continues. --fresh resets.
def _done_set():
    try:
        return set(DONE.read_text().split())
    except Exception:
        return set()


def _mark_done(unit):
    with open(DONE, "a") as fh:
        fh.write(unit + "\n")


def _append_raw(findings):
    if not findings:
        return
    with open(RAW, "a") as fh:
        for f in findings:
            fh.write(json.dumps(f) + "\n")


def _raw_findings():
    out = []
    try:
        for line in RAW.read_text().splitlines():
            if line.strip():
                out.append(json.loads(line))
    except Exception:
        pass
    return out

# Named figures for the pre-flight estimate (the ledger bills actual) — never literals at the call site.
_EST_OVERHEAD_CH = 600
_EST_OUT_TOK = 2500
_CHARS_PER_TOKEN = 4
_METERED_MODEL = "kimi-k3"       # the only panel vendor that bills; used ONLY to price the estimate

FINDING_SCHEMA = {
    "type": "object",
    "properties": {"findings": {"type": "array", "items": {"type": "object", "properties": {
        "line": {"type": "integer"}, "severity": {"type": "string"}, "issue": {"type": "string"}},
        "required": ["issue"]}}},
    "required": ["findings"],
}
REVIEW_SYSTEM = (
    "You are a meticulous code reviewer. Report ONLY real DEFECTS — bugs, races, unhandled errors, wrong logic, "
    "resource leaks, security issues, silent-failure paths. Not style, not nits. For each: the line (if known), a "
    'severity (high|medium|low), and a one-sentence issue. Reply JSON: {"findings":[{"line":<int>,"severity":"..",'
    '"issue":".."}]}. Empty findings is a valid answer — do not invent defects.')
VERDICT_SCHEMA = {"type": "object", "properties": {
    "real": {"type": "boolean"}, "why": {"type": "string"}}, "required": ["real"]}


def src_files():
    return sorted(p for p in SRC.glob("*.py") if p.name != "__init__.py" or p.stat().st_size > 200)


def _parse_findings(answer):
    try:
        d = json.loads(answer)
        return [f for f in (d.get("findings") or []) if isinstance(f, dict) and f.get("issue")]
    except Exception:
        return []


def estimate():
    files = src_files()
    tot_in = tot = 0.0
    for p in files:
        in_tok = (p.stat().st_size + _EST_OVERHEAD_CH) // _CHARS_PER_TOKEN
        tot_in += in_tok
        tot += pricing.realtime_cost(_METERED_MODEL, in_tok, _EST_OUT_TOK) or 0.0
    print(f"client source files: {len(files)}  ·  waves of 10 → {-(-len(files) // 10)}")
    print(f"FILE pass metered est: ${tot:.2f} ({len(files)} kimi calls, ~{int(tot_in):,} in-tok; lanes $0)")
    print("SEAM ~$0.4 · REPO ~$0.2 · VERIFY ~$1 → run with --budget 12 for headroom.")
    return 0


def _panel_review(prompt, purpose, budget_left):
    """One panel call → [(vendor, [findings])], plus $ spent. Honest: only OK vendors contribute findings."""
    try:
        r = spendguard.ask(prompt, schema=FINDING_SCHEMA, system=REVIEW_SYSTEM, purpose=purpose,
                           budget_usd=max(0.01, budget_left))
    except spendguard.BudgetRefused:
        return None, 0.0, {}
    out = []
    for res in r.ok_results:
        out.append((res.vendor, _parse_findings(res.text)))
    return out, r.cost, r.by_vendor


def file_pass(files, wave, budget, spent):
    """Each file reviewed by the whole panel, in concurrent waves of `wave`. A wave is checked against the budget
    BEFORE it runs (estimate-first at the wave granularity), so overspend is bounded to one wave. Resumable: files
    already recorded in DONE are skipped, and each file's findings are appended to RAW the moment it finishes."""
    done = _done_set()
    todo = [p for p in files if p.name not in done]
    if len(todo) < len(files):
        print(f"  resume: {len(files) - len(todo)} files already reviewed, {len(todo)} to go")
    for w in range(0, len(todo), wave):
        batch = todo[w:w + wave]
        est = sum(pricing.realtime_cost(_METERED_MODEL, (p.stat().st_size + _EST_OVERHEAD_CH) // _CHARS_PER_TOKEN,
                                        _EST_OUT_TOK) or 0.0 for p in batch)
        if spent[0] + est > budget:
            print(f"  [budget] stopping before a wave: est +${est:.2f} would pass ${budget:.2f} "
                  f"(spent ${spent[0]:.2f}). FILE coverage is partial — reported as such.")
            break
        print(f"  wave: {', '.join(p.name for p in batch)}")

        def _one(p):
            prompt = f"Review this file for real defects — {p.relative_to(REPO)}:\n\n{p.read_text()}"
            panel, cost, cov = _panel_review(prompt, f"review:file:{p.name}", budget - spent[0])
            return p, panel, cost, cov

        with cf.ThreadPoolExecutor(max_workers=len(batch)) as pool:
            for p, panel, cost, cov in pool.map(_one, batch):
                spent[0] += cost
                fs_out = []
                for vendor, fs in (panel or []):
                    for f in fs:
                        fs_out.append({"axis": "file", "file": str(p.relative_to(REPO)), "vendor": vendor,
                                       "line": f.get("line"), "severity": f.get("severity"), "issue": f["issue"]})
                _append_raw(fs_out)                  # persist BEFORE marking done — a crash re-reviews, never drops
                _mark_done(p.name)
                print(f"     {p.name:26} {len(fs_out)} findings  "
                      f"[{','.join(f'{v}:{k}' for v, k in (cov or {}).items())}]  ${cost:.3f}")


def _cluster(files, budget_left):
    """Agentically group the files into review clusters (a seam = writers+readers of one resource; a concept =
    duplicate implementations). Meaning → LLM. Returns [{"why","files":[...]}]."""
    names = "\n".join(f"- {p.name} ({p.stat().st_size}B)" for p in files)
    prompt = ("Group these Python modules of one package into 6-10 REVIEW CLUSTERS for cross-file review. A good "
              "cluster is either a SEAM (every writer AND reader of one shared resource — a table, a file, a "
              "config) or a CONCEPT (several modules implementing the same capability that could DRIFT apart). "
              'Reply JSON: {"clusters":[{"why":"<what binds them>","files":["a.py","b.py",...]}]}.\n\n' + names)
    schema = {"type": "object", "properties": {"clusters": {"type": "array", "items": {"type": "object",
              "properties": {"why": {"type": "string"}, "files": {"type": "array", "items": {"type": "string"}}},
              "required": ["why", "files"]}}}, "required": ["clusters"]}
    try:
        r = spendguard.ask(prompt, vendors=["anthropic:claude-opus-4-8"], schema=schema, n=1,
                           purpose="review:cluster", budget_usd=max(0.01, budget_left))
    except spendguard.BudgetRefused:
        return []
    if not r.answers:
        return []
    try:
        return json.loads(r.answers[0]).get("clusters") or []
    except Exception:
        return []


def seam_pass(files, budget, spent):
    if "__seam__" in _done_set():
        print("  resume: SEAM pass already done, skipping")
        return
    by_name = {p.name: p for p in files}
    clusters = _cluster(files, budget - spent[0])
    print(f"  {len(clusters)} agentic clusters")
    for cl in clusters:
        members = [by_name[n] for n in cl.get("files", []) if n in by_name]
        if len(members) < 2:
            continue
        est = sum(pricing.realtime_cost(_METERED_MODEL, (p.stat().st_size + _EST_OVERHEAD_CH) // _CHARS_PER_TOKEN,
                                        _EST_OUT_TOK) or 0.0 for p in members)
        if spent[0] + est > budget:
            print(f"  [budget] stopping SEAM before a cluster: est +${est:.2f} would pass ${budget:.2f}.")
            return                                   # not marked done → a resume finishes the remaining clusters
        blob = "\n\n".join(f"# ==== {p.name} ====\n{p.read_text()}" for p in members)
        prompt = (f"These modules are one cluster: {cl.get('why')}. Review them TOGETHER for CROSS-FILE defects: "
                  f"contract gaps between a writer and a reader, a producer/consumer that disagree, or the same "
                  f"job implemented two ways that have DRIFTED. Only real defects.\n\n{blob}")
        panel, cost, cov = _panel_review(prompt, "review:seam", budget - spent[0])
        spent[0] += cost
        fs_out = []
        for vendor, fs in (panel or []):
            for f in fs:
                fs_out.append({"axis": "seam", "file": ",".join(p.name for p in members), "vendor": vendor,
                               "line": f.get("line"), "severity": f.get("severity"), "issue": f["issue"]})
        _append_raw(fs_out)
        print(f"     cluster [{', '.join(p.name for p in members)}]: {len(fs_out)} findings  ${cost:.3f}")
    _mark_done("__seam__")


def repo_pass(files, budget, spent):
    """Axis 4 — hand the panel the file MAP and ask what discipline is claimed but MISSING somewhere."""
    if "__repo__" in _done_set():
        print("  resume: REPO pass already done, skipping")
        return
    est = pricing.realtime_cost(_METERED_MODEL, 6000, _EST_OUT_TOK) or 0.0
    if spent[0] + est > budget:
        print("  [budget] skipping REPO pass — no headroom.")
        return
    mp = "\n".join(f"- {p.name} ({p.stat().st_size}B)" for p in files)
    prompt = ("Here is the file map of one Python package (a pre-submit LLM SPEND GATE + ledger + reconcilers). "
              "Name REPO-WIDE INVARIANT defects — a discipline this kind of system MUST hold that is likely "
              "ABSENT or inconsistent across these files: e.g. every mutation backed up before it runs, every "
              "money write audited, every external call gated, no silent failure on a spend path. Only real, "
              f"likely-missing invariants.\n\n{mp}")
    panel, cost, cov = _panel_review(prompt, "review:repo", budget - spent[0])
    spent[0] += cost
    fs_out = []
    for vendor, fs in (panel or []):
        for f in fs:
            fs_out.append({"axis": "repo", "file": "(repo-wide)", "vendor": vendor,
                           "line": f.get("line"), "severity": f.get("severity"), "issue": f["issue"]})
    _append_raw(fs_out)
    _mark_done("__repo__")
    print(f"     repo-wide: {len(fs_out)} candidate invariant findings  ${cost:.3f}")


def verify(findings, budget, spent):
    """Adversarially verify each finding (refute-by-default) with one strong judge on the $0 lane, CONCURRENTLY
    (the governor bounds the lane). Keep the reals AND the unverifiable (flagged) — drop only the refuted."""
    def _v(f):
        ctx = ""
        fp = SRC / pathlib.Path(f["file"].split(",")[0]).name
        if fp.exists():
            ctx = fp.read_text()[:8000]
        prompt = (f"A reviewer claims this defect in {f['file']}"
                  + (f" (line {f['line']})" if f.get("line") else "") + f":\n\"{f['issue']}\"\n\n"
                  f"Here is the file (maybe truncated):\n{ctx}\n\nIs this a REAL defect? Default to real=false if "
                  'you cannot confirm it from the code. Reply JSON: {"real":true|false,"why":"<one sentence>"}.')
        real = None
        try:
            r = spendguard.ask(prompt, vendors=["anthropic:claude-opus-4-8"], n=1, schema=VERDICT_SCHEMA,
                               purpose="review:verify", budget_usd=max(0.01, budget - spent[0]))
            spent[0] += r.cost                       # lane → ~$0; racy increment is harmless at this scale
            if r.answers:
                real = bool(json.loads(r.answers[0]).get("real"))
        except Exception:
            real = None                              # could not verify → keep it, flagged UNVERIFIED
        f["verified"] = real
        return f
    confirmed = []
    with cf.ThreadPoolExecutor(max_workers=6) as pool:
        for f in pool.map(_v, findings):
            if f.get("verified") is not False:       # keep real (True) and unverifiable (None); drop refuted (False)
                confirmed.append(f)
    return confirmed


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--phase", choices=["file", "seam", "repo", "all"], default="all")
    ap.add_argument("--wave", type=int, default=10)
    ap.add_argument("--budget", type=float, default=12.0, help="hard metered-$ ceiling (kimi only)")
    ap.add_argument("--estimate", action="store_true", help="zero-spend count + $ estimate, then exit")
    ap.add_argument("--fresh", action="store_true", help="discard prior progress (RAW/DONE/OUT) and start over")
    a = ap.parse_args(argv)
    if a.estimate:
        return estimate()
    if a.fresh:
        for p in (RAW, DONE, OUT):
            try:
                p.unlink()
            except OSError:
                pass

    files = src_files()
    spent = [0.0]
    t0 = time.time()
    if a.phase in ("file", "all"):
        print(f"\n== FILE pass ({len(files)} files, waves of {a.wave}) ==")
        file_pass(files, a.wave, a.budget, spent)
    if a.phase in ("seam", "all"):
        print("\n== SEAM pass (agentic clusters) ==")
        seam_pass(files, a.budget, spent)
    if a.phase in ("repo", "all"):
        print("\n== REPO pass (axis 4 — absence) ==")
        repo_pass(files, a.budget, spent)

    done = _done_set()
    incomplete = ((a.phase in ("file", "all") and any(p.name not in done for p in files))
                  or (a.phase in ("seam", "all") and "__seam__" not in done)
                  or (a.phase in ("repo", "all") and "__repo__" not in done))
    if incomplete:
        ndone = len(done & {p.name for p in files})
        print(f"\n[paused] gather partial ({ndone}/{len(files)} files, ${spent[0]:.2f} spent) — budget or interrupt. "
              f"Re-run the SAME command to resume where it left off; VERIFY runs once gather completes.")
        return 0

    raw = _raw_findings()
    print(f"\n== VERIFY ({len(raw)} raw findings, adversarial refute-by-default) ==")
    confirmed = verify(raw, a.budget, spent)

    OUT.write_text("\n".join(json.dumps(f) for f in confirmed) + "\n")
    hi = [f for f in confirmed if (f.get("severity") or "").lower() == "high"]
    print(f"\n=== {len(confirmed)} confirmed defects ({len(hi)} high) · ${spent[0]:.2f} metered · {time.time()-t0:.0f}s ===")
    for f in sorted(confirmed, key=lambda x: {"high": 0, "medium": 1, "low": 2}.get((x.get("severity") or "").lower(), 3)):
        v = "" if f.get("verified") else "  (UNVERIFIED)"
        print(f"  [{(f.get('severity') or '?'):6}] {f['axis']:4} {f['file']}"
              + (f":{f['line']}" if f.get("line") else "") + f" — {f['issue']}{v}")
    print(f"\nfull findings → {OUT}")
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
