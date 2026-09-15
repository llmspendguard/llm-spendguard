"""`spendguard comprehend` — fan a CORPUS of files across the $0 subscription lanes for comprehension /
doc-mining / gap-analysis, INSTEAD of spawning Claude-only sub-agents.

WHY THIS EXISTS (the biggest cost leak in agentic coding). An Agent-tool sub-agent doing comprehension runs
ONLY on the Anthropic plan: it bills the Max plan, hits weekly overage, and cannot touch the codex / gemini /
zai plans. The SAME comprehension, expressed as a spendguard-gated fan of one task per file, is $0 on another
subscription and drains the exhausted Claude lane not at all. `lane_balance.bulk_delegate` already does the
durable, quota-aware, tail-hedged fan (excluding a lane that is cooling or out of quota); this wraps it with
corpus loading, an estimate-first pre-flight, and per-file aggregation. See the `spendguard doctor` overage
nudge and the install-rule doctrine that point here.

DISCIPLINES (inherited, non-negotiable):
  • WHOLE evidence — each file is read and analysed in FULL; a file too large for one task is CHUNKED into
    contiguous parts (CONTAINMENT, never a silent cut), and every part is analysed, so nothing a judgement
    reads is dropped.
  • ESTIMATE-FIRST — the default is a ZERO-SPEND estimate built from the corpus's REAL measured size (never
    invented token counts); `--run` executes and refuses over `--budget-usd`.
  • RUN UNDER THE GATE — the run path fails closed if the interpreter is not gated.
  • CHUNK-never-single-shot — the fan ALWAYS runs against a resumable checkpoint (auto by default), so a crash
    mid-corpus resumes instead of re-paying, and the per-file outputs are never held in a single copy.
  • ATTRIBUTED — every call is tagged with the caller's `intent`, so the spend records under the right job.
"""
import glob as _glob
import os
import re

from . import calls, config, expected_output, lane_balance, pricing

# Per-task evidence ceiling (chars). A file larger than this is split into contiguous parts — contained, never
# cut. ~200k chars ≈ 50k tokens, comfortably inside a 1M-context model, so in practice most files are one task.
_DEFAULT_MAX_CHARS = 200_000
# Coarse chars→tokens proxy for the PRE-SPEND estimate. This measures the real byte size of the corpus (not an
# invented token count); the true cost is whatever the lanes/metered path actually bill, trued up by reconcile.
_CHARS_PER_TOKEN = 4
_DEFAULT_QUESTION = ("Read the file below and extract its key points, decisions, and any gaps, risks, or "
                     "inconsistencies. Be specific and cite the relevant lines.")


def resolve_files(patterns):
    """Every existing FILE named by `patterns` (globs expanded with `**` recursion, exact paths kept, de-duped,
    sorted). A pattern that matches nothing is simply absent from the result — the caller reports that, so a
    typo'd glob never silently looks like an empty corpus success."""
    seen, out = set(), []
    for pat in patterns:
        p = os.path.expanduser(pat)
        matches = _glob.glob(p, recursive=True)
        if not matches and os.path.exists(p):
            matches = [p]
        for m in sorted(matches):
            rp = os.path.abspath(m)
            if os.path.isfile(m) and rp not in seen:
                seen.add(rp)
                out.append(m)
    return out


def chunk_contained(text, max_chars):
    """Split `text` into contiguous parts each <= max_chars, preferring a newline boundary so a part is never
    cut mid-line. CONTAINMENT, not truncation: every character lands in exactly one part and nothing is
    dropped — the sanctioned way to handle evidence too large for one call (chunk, never cut)."""
    if len(text) <= max_chars:
        return [text]
    parts, i = [], 0
    while i < len(text):
        end = min(i + max_chars, len(text))
        if end < len(text):
            nl = text.rfind("\n", i, end)
            if nl > i:
                end = nl + 1
        parts.append(text[i:end])
        i = end
    return parts


def build_tasks(files, question, max_chars=_DEFAULT_MAX_CHARS):
    """One task per file, or per contiguous chunk of a file too large for `max_chars`. Each task carries the
    WHOLE chunk as evidence plus the question. Returns a list of dicts:
    {key, file, part, parts, prompt|None, chars, error?}. An unreadable file becomes an error task (surfaced,
    never a silent omission)."""
    tasks = []
    for f in files:
        try:
            with open(f, errors="replace") as fh:
                text = fh.read()
        except OSError as e:
            tasks.append({"key": f, "file": f, "part": 1, "parts": 1, "prompt": None, "chars": 0,
                          "error": f"{type(e).__name__}: {str(e)[:80]}"})
            continue
        parts = chunk_contained(text, max_chars)
        for i, chunk in enumerate(parts):
            label = f"{f}  (part {i + 1}/{len(parts)})" if len(parts) > 1 else f
            prompt = f"{question}\n\n--- FILE: {label} ---\n{chunk}"
            tasks.append({"key": (f"{f}#{i + 1}" if len(parts) > 1 else f), "file": f, "part": i + 1,
                          "parts": len(parts), "prompt": prompt, "chars": len(chunk)})
    return tasks


def default_checkpoint(intent):
    """A STABLE per-intent checkpoint path under ~/.spendguard/comprehend. Stable (not timestamped) so re-running
    the same intent RESUMES — bulk_delegate keys resume by content hash, so files already analysed are not
    re-paid and a crash mid-corpus never loses the outputs already gathered. This jsonl is the DURABLE primary
    copy of the results; any `--out` export is derived from it. The intent is sanitised to a safe filename
    (mechanical character substitution — a filename transform, not a decision about meaning)."""
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", intent) or "comprehend"
    d = config.HOME / "comprehend"
    d.mkdir(parents=True, exist_ok=True)
    return str(d / f"{safe}.jsonl")


def estimate_corpus(tasks, model, intent):
    """ZERO-SPEND cost ceiling from the MEASURED corpus. Input tokens come from the real character counts of the
    files (measurement, not an invented literal); per-task output comes from expected_output.expect (a learned
    p90 for this intent, else this model's measured history, else its published ceiling — its `basis` is
    reported). It is a CEILING for the metered-fallback worst case; the lanes bill $0, so the real spend is
    typically far below it and reconcile trues it up."""
    live = [t for t in tasks if t.get("prompt")]
    in_tok = sum(max(1, t["chars"] // _CHARS_PER_TOKEN) for t in live)
    per_out, basis = expected_output.expect(model, sig=intent)
    # cost_or_unpriced returns 0.0 (and RECORDS the model as unpriced) ONLY for an unpriced model / bad args
    # (KeyError/TypeError/ValueError). A deliberate stop — EstimateNotGrounded, SpendGateRefused — PROPAGATES
    # through it: a refusal must never be downgraded to a $0 ceiling that would wave the budget check through.
    total = pricing.cost_or_unpriced(model, in_tok, per_out * len(live), batch=False)
    return {"model": model, "tasks": len(live), "in_tok": in_tok, "out_tok_per_task": per_out,
            "out_basis": basis, "est_usd_ceiling": round(total, 4)}


def _aggregate(tasks, keyed):
    """Fold the per-task rows back onto their files, in part order. Returns {file: {parts, text, lanes, billed,
    errors}} — `text` is the parts joined, `lanes` the distinct lanes that served it, `billed` whether any part
    fell back to the metered API, `errors` any parts that failed."""
    by_file = {}
    for t in tasks:
        if not t.get("prompt"):
            by_file.setdefault(t["file"], {"parts": 0, "text": "", "lanes": [], "billed": False,
                                           "errors": [t.get("error", "unreadable")]})
            continue
        row = keyed.get(t["key"]) or {}
        agg = by_file.setdefault(t["file"], {"parts": 0, "text": "", "lanes": [], "billed": False, "errors": []})
        agg["parts"] += 1
        piece = row.get("text") or ""
        agg["text"] += (("\n\n" if agg["text"] else "") + piece)
        lane = row.get("lane")
        if lane and lane not in agg["lanes"]:
            agg["lanes"].append(lane)
        if row.get("billed"):
            agg["billed"] = True
        if row.get("error"):
            agg["errors"].append(f"part {t['part']}: {row['error']}")
    return by_file


def comprehend_corpus(patterns, intent, question=None, model=None, run=False, budget_usd=None, lanes=None,
                      max_chars=_DEFAULT_MAX_CHARS, checkpoint=None):
    """Fan the corpus across the $0 lanes and aggregate per file. Default (run=False) is a ZERO-SPEND estimate.

    `intent` is the job-type label every call records under (required — attribution is the core mission).
    `lanes` optionally confines the fan to an explicit lane subset (e.g. ["codex", "gemini"] to keep it off a
    protected lane); None lets bulk_delegate use every eligible idle lane, which already excludes a lane that is
    out of quota. `checkpoint` is the resumable results jsonl — when None on a run, a STABLE per-intent path is
    used, so the fan is durable/resumable by default. `run=True` FAILS CLOSED if the interpreter is not gated,
    and refuses if the estimate exceeds `budget_usd`."""
    question = question or _DEFAULT_QUESTION
    model = model or config.advisor_model()
    files = resolve_files(patterns)
    tasks = build_tasks(files, question, max_chars=max_chars)
    live = [t for t in tasks if t.get("prompt")]
    unreadable = [t for t in tasks if not t.get("prompt")]
    est = estimate_corpus(tasks, model, intent)
    base = {"files": len(files), "tasks": len(live), "unreadable": [t["file"] for t in unreadable],
            "estimate": est, "intent": intent, "model": model}
    if not files:
        return {**base, "error": "no files matched the given patterns"}
    if not run:
        return {**base, "ran": False, "note": "estimate only — re-run with run=True (CLI: --run) to fan across "
                "the $0 lanes under the gate."}

    import spendguard
    spendguard.require()                       # fail closed — the fan spends (a lane miss falls back to metered)
    if budget_usd is not None and est["est_usd_ceiling"] > float(budget_usd):
        return {**base, "ran": False, "refused": f"estimate ceiling ${est['est_usd_ceiling']:.4f} exceeds "
                f"--budget-usd ${float(budget_usd):.4f}"}
    if not live:
        return {**base, "ran": False, "error": "no readable files to analyse"}

    if not checkpoint:                         # durable + resumable by default (chunk-never-single-shot): each
        checkpoint = default_checkpoint(intent)  # result is appended before the next chunk, so a crash resumes
    prompts = [t["prompt"] for t in live]
    keys = [t["key"] for t in live]            # unique per task (file or file#part) → pair results by MEANING
    with calls.context(intent=intent):
        keyed = lane_balance.bulk_delegate(prompts, intent=intent, lanes=lanes, checkpoint=checkpoint,
                                           task_key=keys, return_keyed=True)
    rows = keyed if isinstance(keyed, dict) else {}
    by_file = _aggregate(tasks, rows)
    served = sum(1 for v in rows.values() if isinstance(v, dict) and v.get("text"))
    billed = [f for f, a in by_file.items() if a["billed"]]
    return {**base, "ran": True, "served": served, "results": by_file,
            "billed_files": billed, "checkpoint": checkpoint}


def cmd(argv=None):
    """`spendguard comprehend <globs…> --intent <job> [--question Q] [--run] [--budget-usd X] [--lanes a,b]
    [--max-chars N] [--model M] [--checkpoint P] [--out results.jsonl]`. Estimate-first: no --run prints the
    zero-spend estimate; --run fans across the $0 lanes against a resumable checkpoint."""
    import argparse
    import sys
    argv = list(sys.argv[2:] if argv is None else argv)
    ap = argparse.ArgumentParser(prog="spendguard comprehend", description="fan a corpus across the $0 lanes")
    ap.add_argument("patterns", nargs="+", help="files or globs to analyse (e.g. 'docs/**/*.md')")
    ap.add_argument("--intent", required=True, help="job-type label the spend records under (attribution)")
    ap.add_argument("--question", help="what to ask of each file (default: extract key points + gaps/risks)")
    ap.add_argument("--run", action="store_true", help="actually fan across the lanes (default: zero-spend estimate)")
    ap.add_argument("--budget-usd", type=float, help="refuse the run if the estimate ceiling exceeds this")
    ap.add_argument("--lanes", help="confine the fan to these lanes, comma-separated (e.g. codex,gemini)")
    ap.add_argument("--max-chars", type=int, default=_DEFAULT_MAX_CHARS,
                    help=f"per-task evidence ceiling; larger files are chunked, never cut (default {_DEFAULT_MAX_CHARS})")
    ap.add_argument("--model", help="pin the model (default: config.advisor_model)")
    ap.add_argument("--checkpoint", help="resumable results jsonl (default: ~/.spendguard/comprehend/<intent>.jsonl)")
    ap.add_argument("--out", help="also export the aggregated per-file results as JSON to this path")
    a = ap.parse_args(argv)
    lanes = [x.strip() for x in a.lanes.split(",") if x.strip()] if a.lanes else None
    res = comprehend_corpus(a.patterns, intent=a.intent, question=a.question, model=a.model, run=a.run,
                            budget_usd=a.budget_usd, lanes=lanes, max_chars=a.max_chars, checkpoint=a.checkpoint)
    est = res["estimate"]
    print(f"comprehend — {res['files']} file(s) → {res['tasks']} task(s), model {est['model']} · "
          f"estimate ceiling ${est['est_usd_ceiling']:.4f} "
          f"(~{est['in_tok']} in tok, {est['out_tok_per_task']}/task out, basis={est['out_basis']})")
    if res.get("unreadable"):
        print(f"  ⚠ {len(res['unreadable'])} unreadable file(s): {', '.join(res['unreadable'][:5])}")
    if res.get("error"):
        print(f"  🔴 {res['error']}")
        return 2
    if res.get("refused"):
        print(f"  🔴 REFUSED — {res['refused']}")
        return 2
    if not a.run:
        print("  ESTIMATE ONLY — re-run with --run to fan across the $0 lanes (drains the exhausted Claude plan "
              "not at all; a lane miss falls back to that provider's metered API).")
        return 0
    lanes_used = sorted({ln for agg in res["results"].values() for ln in agg["lanes"]})
    print(f"  fanned {res['served']}/{res['tasks']} served · lanes: {', '.join(lanes_used) or '(none)'} · "
          f"billed (metered-fallback) file(s): {len(res['billed_files'])}")
    print(f"  durable results (resumable) → {res['checkpoint']}")
    if a.out:
        d = os.path.dirname(a.out)
        if d:
            os.makedirs(d, exist_ok=True)
        # the sanctioned whole-file JSON writer: atomic, keeps a `~` backup, and REFUSES to clobber an existing
        # file that does not parse — never a hand-rolled open(...,'w')+json.dump (see test_every_json_write_backs_up)
        config.update_json(a.out, lambda _prev: res["results"])
        print(f"  also exported aggregated per-file results → {a.out}")
    return 0
