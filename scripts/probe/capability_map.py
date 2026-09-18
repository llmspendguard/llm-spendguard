#!/usr/bin/env python
"""capability_map.py — rebuild the CONCEPT/SEAM axis (CLAUDE.md #0b): for each CAPABILITY, find every function that
implements part of it and whether they AGREE (one canonical owner + thin delegators), DRIFT (copies re-implementing
one idea — the bug factory), or are a legitimate PROTOCOL (a uniform contract each must implement, e.g. per-lane
run_prompt). Answers 'how many paths do we have for one idea, and which ones disagree?'.

Two-phase, small+large convergence loop (the repo's own pattern):
  Phase A — MECHANICAL: AST-extract every top-level function + class method (module::name, FULL body). Parsing, not
            a meaning decision. Then AGENTIC: fan the WHOLE bodies across $0 subscription lanes (governed by the
            dispatch governor, durable checkpoint/resume) — each function gets a CAPABILITY tag + role FROM ITS BODY
            (never its name; CLAUDE.md: cluster from bodies). $0 (plan-served), schema-validated.
  Phase B — AGENTIC: group by capability; for each capability with >1 substantive impl, OPUS adjudicates canonical
            OWNER vs DRIFT vs PROTOCOL, reading the FULL bodies (never truncated). This is the metered spend.

DURABLE: both phases append to a resumable checkpoint (labels + verdicts); a re-run RESUMES (never re-spends opus or
wipes prior verdicts), and the .md/.json summaries are regenerated atomically (temp + os.replace) from those.
HONEST COVERAGE: an unlabelable function and an unadjudicated group are NAMED + COUNTED (UNLABELED / UNADJUDICATED),
never silently dropped — a lost unit would read as 'no duplication', the exact absence-as-success failure this catches.

  ./.venv.nosync/bin/python scripts/probe/capability_map.py --estimate          # count + plan, spend $0
  ./.venv.nosync/bin/python scripts/probe/capability_map.py --run [--limit N]    # execute (validate on N first); resumable
  ./.venv.nosync/bin/python scripts/probe/capability_map.py --run --all          # whole src/spendguard, not just the core surface
"""
import argparse
import ast
import collections
import json
import os
import sys

import spendguard
spendguard.require()
from spendguard import lane_balance, adapters, config, calls

_HERE = os.path.dirname(os.path.abspath(__file__))
_SRC = os.path.normpath(os.path.join(_HERE, "..", "..", "src", "spendguard"))
_OUT = os.path.join(_HERE, "capability_map_out")
# The CORE call/spend/record surface — where this session's robustness bugs lived (multi-path concepts). --all widens
# to every module. Named here (never a literal scattered in the code); a file that is absent is skipped with a notice.
_CORE = ["adapters.py", "vendor_call.py", "gate.py", "bulkgate.py", "pricing.py", "dispatch.py",
         "zai_exec.py", "codex_exec.py", "subscription_exec.py", "antigravity_exec.py",
         "calls.py", "resource_state.py", "lane_balance.py", "reconcile.py", "ledger_sync.py"]

_LABEL_SYS = (
    "You map a codebase's CAPABILITIES to find DUPLICATION — many functions implementing one idea. You are given ONE "
    "function: its module::name and its FULL body. Decide FROM THE BODY, never the name:\n"
    "- capability: a short canonical kebab-case label for the ONE idea it implements (e.g. resolve-output-budget, "
    "record-call-outcome, lane-metered-fallback, detect-truncation, resolve-api-key, cool-lane, price-call, "
    "admit-dispatch, resolve-served-model, reconcile-source). Use the SAME label for two functions that do the same "
    "idea, even in different files.\n"
    "- role: 'owner' (the primary/canonical implementation of that capability), 'delegates' (it calls another "
    "function to do the real work), 'inlines' (it RE-IMPLEMENTS logic that likely belongs to a shared owner — a "
    "drift risk), or 'helper' (a small self-contained piece, not the capability itself).\n"
    "Return ONLY minified JSON: {\"id\":\"<echo the given id EXACTLY>\",\"capability\":\"...\",\"role\":\"...\","
    "\"one_line\":\"<=12 words\"}.")

_LABEL_SCHEMA = {
    "type": "object", "required": ["id", "capability", "role"],
    "properties": {
        "id": {"type": "string"}, "capability": {"type": "string"},
        "role": {"type": "string", "enum": ["owner", "delegates", "inlines", "helper"]},
        "one_line": {"type": "string"},
    }}

_ADJ_SYS = (
    "These functions were tagged the SAME capability. Read the FULL bodies and decide the truth of the group:\n"
    "- kind: 'single-owner' (one function does the work, the rest correctly DELEGATE to it), 'protocol' (a uniform "
    "contract each MUST implement separately — e.g. a per-lane run_prompt, a Source.truth_total — legitimately N "
    "copies), or 'DRIFT' (two+ functions RE-IMPLEMENT the same logic and can disagree — the bug factory).\n"
    "- owner: module::name of the canonical owner, or 'none'.\n"
    "- agree: for DRIFT, do the copies currently AGREE in behavior, or have they already diverged (false)?\n"
    "Return ONLY minified JSON: {\"capability\":\"...\",\"kind\":\"single-owner|protocol|DRIFT\",\"owner\":\"...\","
    "\"agree\":true,\"members\":[{\"id\":\"...\",\"verdict\":\"owner|delegates|drift|protocol-impl|helper\","
    "\"why\":\"<=15 words\"}],\"fix\":\"<one sentence: the single brain to route all paths through, if DRIFT>\"}.")

_ADJ_SCHEMA = {
    "type": "object", "required": ["capability", "kind", "members"],
    "properties": {
        "capability": {"type": "string"},
        "kind": {"type": "string", "enum": ["single-owner", "protocol", "DRIFT"]},
        "owner": {"type": "string"}, "agree": {"type": "boolean"},
        "members": {"type": "array"}, "fix": {"type": "string"}}}


def _adjudicator():
    """The opus-class model that adjudicates a capability group (config-driven, never a literal): the same
    adjudicator the bakeoff/requirement-judge use, else the advisor's strong model."""
    for _get in ("advisor_adjudicator_model", "advisor_judge_model"):
        try:
            m = getattr(config, _get)()
            if m:
                return m
        except Exception:
            pass
    return "claude-opus-4-8"


def _atomic_write(path, text):
    """Write via a temp file + os.replace so a crash mid-write never corrupts or empties a prior result."""
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        f.write(text)
    os.replace(tmp, path)


def _collect(files):
    """Every top-level function + class method as (qualname, file, lineno, source). AST parsing only — a mechanical
    extraction of DEFS, never a decision about what they mean (that is the LLM's job below). Nested inner functions
    are skipped: they are local helpers, not the cross-file duplication axis this sweep looks for."""
    out = []
    for f in files:
        path = os.path.join(_SRC, f)
        if not os.path.exists(path):
            print(f"  (skip {f}: not found)", file=sys.stderr)
            continue
        src = open(path).read()
        try:
            tree = ast.parse(src, filename=f)
        except SyntaxError as e:
            print(f"  (skip {f}: {e})", file=sys.stderr)
            continue
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                out.append((f"{f[:-3]}::{node.name}", f, node.lineno, ast.get_source_segment(src, node) or ""))
            elif isinstance(node, ast.ClassDef):
                for m in node.body:
                    if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        out.append((f"{f[:-3]}::{node.name}.{m.name}", f, m.lineno,
                                    ast.get_source_segment(src, m) or ""))
    return out


def _label_task(qn, source):
    return f"id: {qn}\n\n```python\n{source}\n```"


def _phase_a(funcs):
    """Label every function's capability on the $0 lanes. Returns (labels{qn:{cap,role,one_line}}, unlabeled[(qn,why)])
    — an unlabelable unit is NAMED in `unlabeled`, never dropped (a lost unit would read as 'no duplication'). Durable:
    bulk_delegate's checkpoint appends each result, so a re-run resumes rather than re-spending the fan."""
    tasks = [_label_task(qn, src) for (qn, _f, _ln, src) in funcs]
    by_task = {_label_task(qn, src): qn for (qn, _f, _ln, src) in funcs}
    valid = set(by_task.values())
    ck = os.path.join(_OUT, "labels.checkpoint.jsonl")
    print(f"Phase A: labeling {len(tasks)} functions across $0 lanes (best-value; resumable checkpoint {ck}) ...")
    res = lane_balance.bulk_delegate(tasks, intent="capability-map:label", system=_LABEL_SYS, schema=_LABEL_SCHEMA,
                                     checkpoint=ck, deadline_s=150.0, return_keyed=True, tier="cheap",
                                     task_key=lambda t: by_task.get(t, t[:60]))   # cheap tier: labeling is coarse, spread it
    #                                    across the $0 lanes' cheap models — NOT opus (the nuanced owner-vs-drift call below is opus)
    labels, unlabeled = {}, []
    items = res.items() if isinstance(res, dict) else list(enumerate(res))
    for key, r in items:
        qn = key if key in valid else None
        txt = (r or {}).get("text") if isinstance(r, dict) else None
        if not txt:
            unlabeled.append((qn or str(key), (isinstance(r, dict) and r.get("error")) or "no result text"))
            continue
        try:
            j = json.loads(txt)
        except Exception as e:
            unlabeled.append((qn or str(key), f"unparsed label JSON: {str(e)[:40]}"))
            continue
        qn = qn or (j.get("id") if j.get("id") in valid else str(key))
        labels[qn] = {"capability": (j.get("capability") or "?").strip(), "role": j.get("role") or "?",
                      "one_line": j.get("one_line", "")}
    seen = set(labels) | {u[0] for u in unlabeled}
    for q in valid - seen:                                    # a task that produced NO result row at all is still named
        unlabeled.append((q, "no result returned for this task"))
    print(f"  labeled {len(labels)}/{len(funcs)}; {len(unlabeled)} UNLABELED (named, NOT silently dropped):")
    for qn, why in unlabeled:
        print(f"    · UNLABELED {qn}: {why}")
    return labels, unlabeled


def _load_verdicts(vck_path):
    """Resume: prior verdicts from the append-only checkpoint (never re-spend opus on a group already done)."""
    verdicts = {}
    if os.path.exists(vck_path):
        for line in open(vck_path):
            try:
                verdicts.update(json.loads(line))
            except Exception:
                pass
    return verdicts


def _phase_b(candidates, src_by_qn, max_groups):
    """OPUS adjudicates each candidate capability (owner vs DRIFT vs protocol), FULL bodies. Spend is gate-owned;
    a mechanical group-count cap (max_groups) is the only pre-check. RESUMES from an append-only checkpoint (never
    re-spends or wipes a prior verdict). A group that yields no clean verdict is recorded UNADJUDICATED (named), not lost."""
    adj_model = _adjudicator()
    vck_path = os.path.join(_OUT, "verdicts.checkpoint.jsonl")
    verdicts = _load_verdicts(vck_path)
    todo = [(cap, qns) for cap, qns in sorted(candidates.items())
            if cap not in verdicts or verdicts[cap].get("kind") == "UNADJUDICATED"]
    print(f"  {len(verdicts)} verdict(s) resumed from checkpoint; {len(todo)} group(s) to adjudicate")
    # SPEND is owned by the GATE: each adjudication is a gated adapters.call (estimate-first, capped). The only
    # pre-check here is a MECHANICAL group-COUNT bound — a $ estimate would have to INVENT an output length, exactly
    # the invented-token quote the estimate-literals guard forbids. So cap by count and let the gate enforce the $.
    if len(todo) > max_groups:
        print(f"  REFUSED: {len(todo)} groups to adjudicate exceeds --max-groups {max_groups}. Narrow --files or "
              f"raise --max-groups. (The gate enforces the actual per-call $; this is a mechanical count guard.)")
        return None
    print(f"Phase B: adjudicate {len(todo)} groups on {adj_model} (gate-enforced spend) ...")
    with open(vck_path, "a") as vck, calls.context(intent="capability-map:adjudicate"):   # APPEND only — never truncate
        for i, (cap, qns) in enumerate(todo, 1):
            blob = "\n\n".join(f"### {q}\n```python\n{src_by_qn.get(q, '')}\n```" for q in qns)
            r = adapters.call(adj_model, f"capability: {cap}\n\n{blob}", system=_ADJ_SYS, schema=_ADJ_SCHEMA,
                              intent="capability-map:adjudicate", no_substitution=True)
            txt = r.get("text")
            if not txt:
                v = {"capability": cap, "kind": "UNADJUDICATED", "error": r.get("error") or "empty adjudicator reply",
                     "members": [{"id": q} for q in qns]}
            else:
                try:
                    v = json.loads(txt)
                except Exception as e:
                    v = {"capability": cap, "kind": "UNADJUDICATED", "error": f"unparsed verdict JSON: {str(e)[:40]}",
                         "members": [{"id": q} for q in qns]}
            verdicts[cap] = v
            vck.write(json.dumps({cap: v}) + "\n")            # durable: each verdict survives a mid-run crash
            vck.flush()
            print(f"  [{i}/{len(todo)}] {cap}: {v.get('kind', '?')}")
    return verdicts


def _dump(labels, verdicts, unlabeled):
    os.makedirs(_OUT, exist_ok=True)
    _atomic_write(os.path.join(_OUT, "capability_map.json"),
                  json.dumps({"labels": labels, "verdicts": verdicts, "unlabeled": unlabeled}, indent=2))
    order = {"DRIFT": 0, "UNADJUDICATED": 1, "protocol": 2, "single-owner": 3}
    lines = ["# Capability map — one idea, how many paths?", "",
             f"_coverage: {len(labels)} labeled, {len(unlabeled)} UNLABELED, {len(verdicts)} groups adjudicated_", ""]
    for cap, v in sorted(verdicts.items(), key=lambda kv: order.get(kv[1].get("kind"), 4)):
        lines.append(f"## {v.get('kind', '?')}  ·  {cap}")
        if v.get("owner"):
            lines.append(f"- owner: `{v['owner']}`  ·  agree={v.get('agree')}")
        if v.get("fix"):
            lines.append(f"- fix: {v['fix']}")
        if v.get("error"):
            lines.append(f"- NOT ADJUDICATED: {v['error']}")
        for m in (v.get("members") or []):
            lines.append(f"  - `{m.get('id')}` — **{m.get('verdict', '?')}** — {m.get('why', '')}")
        lines.append("")
    if unlabeled:
        lines += ["## UNLABELED (no capability tag — coverage gap, not a clean result)", ""]
        lines += [f"  - `{qn}` — {why}" for qn, why in unlabeled] + [""]
    _atomic_write(os.path.join(_OUT, "capability_map.md"), "\n".join(lines))
    drift = [c for c, v in verdicts.items() if v.get("kind") == "DRIFT"]
    unadj = [c for c, v in verdicts.items() if v.get("kind") == "UNADJUDICATED"]
    print(f"\nWROTE {os.path.join(_OUT, 'capability_map.md')}\n      {os.path.join(_OUT, 'capability_map.json')}")
    print(f"\n{len(drift)} DRIFT (many paths, one idea): {', '.join(sorted(drift)) or 'none'}")
    print(f"{len(unadj)} UNADJUDICATED (re-run to resolve): {', '.join(sorted(unadj)) or 'none'}")


def _run(funcs, max_groups):
    os.makedirs(_OUT, exist_ok=True)
    labels, unlabeled = _phase_a(funcs)
    src_by_qn = {qn: src for (qn, _f, _ln, src) in funcs}
    groups = collections.defaultdict(list)
    for qn, lab in labels.items():
        groups[lab["capability"]].append(qn)
    # A capability is a drift candidate when it has TWO+ substantive implementations (a genuine multi-path) OR any
    # member the labeler tagged 'inlines' — that role IS the agentic verdict that the function RE-IMPLEMENTS logic
    # belonging to a shared owner, a drift risk even alone (e.g. duplicating a function defined outside the scanned
    # files). The agentic role decides; the count only recognises the plain multi-copy case.
    def _multi(qns):
        return sum(1 for q in qns if labels[q]["role"] != "helper") >= 2
    candidates = {cap: qns for cap, qns in groups.items()
                  if _multi(qns) or any(labels[q]["role"] == "inlines" for q in qns)}
    print(f"  {len(groups)} capabilities; {len(candidates)} have >1 substantive implementation (drift candidates)")
    verdicts = _phase_b(candidates, src_by_qn, max_groups)
    if verdicts is None:                                       # count-cap refusal — still write the labels + coverage
        _dump(labels, _load_verdicts(os.path.join(_OUT, "verdicts.checkpoint.jsonl")), unlabeled)
        return 2
    _dump(labels, verdicts, unlabeled)
    return 0


def main():
    ap = argparse.ArgumentParser(prog="capability_map")
    ap.add_argument("--run", action="store_true", help="execute (default: estimate only, spend $0)")
    ap.add_argument("--all", action="store_true", help="every module in src/spendguard (default: the core surface)")
    ap.add_argument("--files", nargs="*", help="explicit file basenames to scan (overrides --all/core)")
    ap.add_argument("--limit", type=int, help="scan only the first N functions (validate the pipeline before scaling)")
    ap.add_argument("--max-groups", type=int, default=80, help="refuse Phase B if more than N capability groups need "
                    "adjudication (a mechanical count guard; the GATE enforces the actual per-call spend)")
    a = ap.parse_args()
    files = a.files or (sorted(f for f in os.listdir(_SRC) if f.endswith(".py")) if a.all else _CORE)
    funcs = _collect(files)
    if a.limit:
        funcs = funcs[:a.limit]
    print(f"{len(funcs)} functions across {len(files)} file(s){' (limited)' if a.limit else ''}.")
    if not a.run:
        chars = sum(len(src) for _q, _f, _l, src in funcs)
        print(f"ESTIMATE ONLY. Phase A = {len(funcs)} label calls on $0 lanes (~{chars // 4} tok in, $0 plan-served; "
              f"a lane miss falls back to metered). Phase B = opus adjudication, gate-enforced spend. Re-run with --run.")
        return 0
    return _run(funcs, a.max_groups)


if __name__ == "__main__":
    sys.exit(main())
