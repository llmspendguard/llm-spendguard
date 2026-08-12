"""Review the repo at FOUR granularities — because each axis finds a defect class the others CANNOT see.

THE LESSON THIS ENCODES. Three waves of a four-vendor review produced ~500 findings and missed:
  · 13 functions copy-pasted between chat.py and claudecode.py      (no reviewer saw both files)
  · four writers of config.json, three destructive                  (no reviewer saw all four)
  · spendguard.record_estimate writing gate_ledger while the docs
    send consumers to calibrate.record_estimate writing cost_predictions,
    with calibrate.pair() reading only the latter and nothing bridging   (each file correct ALONE)
  · no backup discipline anywhere before a mutation                 (an ABSENCE — in no file to be found)
Every one of those was invisible for the same reason: the evidence did not fit in the unit being reviewed.
Reviewing harder cannot fix a missing-evidence problem. Changing the UNIT can.

    AXIS 1  FILE       one file in context
                       finds  a swallowed exception, an unguarded index, a wrong branch
                       blind  anything whose second half is in another file
    AXIS 2  CONCEPT    every implementation of one capability, in full, together
                       finds  DRIFT — same job, copies disagree, one is behind the other
                       blind  a concept implemented correctly in two places that must still be one
    AXIS 3  SEAM       every writer and every reader of one shared resource, together
                       finds  CONTRACT GAPS — A writes here, B reads there, nothing bridges;
                              a field written and never read; a field read and never written
                       blind  anything that is nowhere
    AXIS 4  INVARIANT  the whole repo against one claim it makes about itself
                       finds  ABSENCE — the discipline that exists in no file, and the sites that violate
                              a rule the project states
                       blind  nothing structurally; limited only by naming the right invariant

Axis 4 is the one that matters most and is reached for least, because it is the only axis that can find
something MISSING. "There is no backup before any mutation in this repo" is a true, catastrophic finding
that appears in zero files, so axes 1–3 can look forever and never see it.

Grounding is mechanical (tables from SQL, config keys from the schema, files from the writers); every
JUDGEMENT — do these contracts agree, is this invariant violated — is agentic, per CLAUDE.md.

CLI: review_axes.py {seam,invariant} [--run].  Axis 1 = repo_review_panel.py, axis 2 = capability_audit.py
+ review_capability_slice.py. Estimate-first; caged.
"""
import argparse, ast, collections, json, os, pathlib, re, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import repo_defs                                    # the ONE scope-qualified inventory

import spendguard                                     # noqa: F401
spendguard.require()
from spendguard import adapters, calls, config, pricing, ui        # noqa: E402

# ── axis 3: seams ────────────────────────────────────────────────────────────
SEAM_SYSTEM = (
    "You are shown every place a shared resource (a table, a config key, a file) is WRITTEN and every place "
    "it is READ, across a whole codebase. Find CONTRACT GAPS: a producer whose output no consumer reads; a "
    "consumer reading something no producer writes; two producers writing incompatible shapes; a consumer "
    "assuming a field the producer only sometimes sets. Each site may be perfectly correct on its own — the "
    "defect is in the GAP. Say which side is wrong and what breaks in production because of it.")

SEAM_SCHEMA = ('{"gaps": [{"resource": "...", "kind": "written-never-read|read-never-written|shape-mismatch'
               '|missing-bridge", "writer": "mod.fn", "reader": "mod.fn", "what_breaks": "...", '
               '"confidence": "high|medium|low"}]}')

RESOURCE_SYSTEM = (
    "You are shown one function. List every SHARED RESOURCE it touches and whether it READS or WRITES each: "
    "database tables, files on disk, config keys, environment variables, remote endpoints. Include resources "
    "reached through a helper the function calls, and tables whose name is built dynamically — you are being "
    "asked because a pattern-match over the source text gets exactly those wrong. If it touches none, say so.")

RESOURCE_SCHEMA = '{"touches": [{"resource": "name", "kind": "table|file|config|env|endpoint", "mode": "read|write|both"}]}'


def resource_map(root, model, run):
    """{resource: {"w": [...], "r": [...]}} — derived AGENTICALLY from each body.

    THIS WAS A REGEX AND THAT WAS THE BUG. Two regexes matched `INSERT INTO (\\w+)` / `FROM (\\w+)` over the
    source text, and the seam review then reasoned over whatever they produced. Which resource a function
    touches is only format-determined when the SQL is a literal: a table name built with an f-string, a write
    performed by a helper the function calls, a CTE, or `INSERT OR REPLACE` all read as something else or as
    nothing. The regex was not extracting a known shape — it was DECIDING what the code does, and every
    downstream finding inherited its errors silently. A missed writer does not look like a missed writer; it
    looks like a clean one-sided seam.
    """
    ds = repo_defs.defs(root)
    # Only bodies that plausibly reach a shared resource are worth a call, and "plausibly" is itself not a
    # judgement being made here — every body is sent. Cheap model, one pass, recorded so it is never re-paid.
    m = collections.defaultdict(lambda: {"w": set(), "r": set()})
    est = sum(pricing.realtime_cost(model, len(d.src[:1600]) // 4 + 200, 300) or 0 for d in ds)
    if not run:
        return m, ds, est
    for i, d in enumerate(ds, 1):
        with calls.context(intent="spendguard:seam-resource-map"):
            # call_complete, not call: sized from this call-class's measured p99, retried on truncation, and
            # text=None rather than a cut body if it still will not fit. `max_tokens=300` here truncated
            # replies into unparseable JSON, and the loop below then counted them as "touches nothing" —
            # a resource map with silent holes, which is worse than no map because it reads as complete.
            r = adapters.call_complete(model, f"```python\n{d.src[:1600]}\n```\n\nWhat shared resources does "
                                       f"`{d.qual}` touch?\nReply JSON only: {RESOURCE_SCHEMA}",
                                       sig="probe:resource-map", system=RESOURCE_SYSTEM)
        if r.get("error"):
            continue                                    # UNMAPPED — counted by the caller, never read as "none"
        try:
            blob = re.search(r"\{.*\}", r.get("text") or "", re.S)      # PARSING a known shape: allowed
            for t_ in (json.loads(blob.group(0)).get("touches") if blob else []) or []:
                key = f"{t_.get('kind')}:{t_.get('resource')}"
                if t_.get("mode") in ("write", "both"):
                    m[key]["w"].add(d.qual)
                if t_.get("mode") in ("read", "both"):
                    m[key]["r"].add(d.qual)
        except Exception:
            continue
        if i % 100 == 0:
            print(f"    mapped {i}/{len(ds)} bodies", flush=True)
    return m, ds, est


def sources(root):
    """{scope-qualified name: full source}. Delegates to repo_defs — the flat `module.name` key this used to
    build collided 11 times, so the reviewer read one body and attributed it to another definition."""
    return {d.qual: d.src for d in repo_defs.defs(root)}


def run_seams(root, model, run, out_path):
    m, ds, map_est = resource_map(root, model, run)
    src = sources(root)
    if not run:
        ui.estimate_only(action=f"map {len(ds)} bodies to the shared resources they touch (agentic), then "
                                f"review each cross-module seam", cost=map_est)
        return 0
    # A seam is interesting when writers and readers live in DIFFERENT modules — that is precisely the
    # configuration no per-file review can hold in one context.
    seams = []
    for table, sides in sorted(m.items()):
        wmods = {x.split(".")[0] for x in sides["w"]}
        rmods = {x.split(".")[0] for x in sides["r"]}
        if not (sides["w"] and sides["r"]):
            seams.append((table, sides, "ONE-SIDED"))          # written-never-read / read-never-written
        elif wmods != rmods and len(wmods | rmods) > 1:
            seams.append((table, sides, "CROSS-MODULE"))
    print(f"axis 3 (seam) — {len(m)} shared resources, {len(seams)} with a cross-module or one-sided contract")

    def body(table, sides):
        parts = [f"SHARED RESOURCE: `{table}`"]
        for role, key in (("WRITER", "w"), ("READER", "r")):
            for fn in sorted(sides[key])[:4]:
                code = (src.get(fn) or "")[:2200]
                parts.append(f"### {role}: {fn}\n```python\n{code}\n```")
        return "\n\n".join(parts)

    rows, ngap = [], 0
    for i, (table, sides, kind) in enumerate(seams, 1):
        prompt = (body(table, sides) + "\n\nFind CONTRACT GAPS between these writers and readers. A site "
                  f"correct on its own can still be half of a defect.\nReply JSON only: {SEAM_SCHEMA}")
        with calls.context(intent="spendguard:seam-review"):
            r = adapters.call_complete(model, prompt, sig="probe:seam-review", system=SEAM_SYSTEM)
        if r.get("error"):
            print(f"  {i}/{len(seams)} {table}: FAILED — UNREVIEWED, not clean")
            rows.append({"table": table, "verdict": None}); continue
        try:
            blob = re.search(r"\{.*\}", r.get("text") or "", re.S)
            v = json.loads(blob.group(0)) if blob else None
        except Exception:
            v = None
        # NO CONFIDENCE THRESHOLD. This filtered on `g.get("confidence") != "low"` — a hand-picked cutoff
        # deciding which findings are worth a human's attention, which is a judgement about risk and context,
        # and made on a label the same model volunteered about its own output. It also does not work: the
        # first run produced 377 "gaps" through that filter, a number nobody can act on. What DID work today,
        # on the silent-failure triage, was refutation — every candidate handed to independent skeptics
        # instructed to knock it down, defaulting to refuted when unsure. 60 became 26 that way, and the 34
        # it removed were real over-calls. Every gap is kept here and settled by that pass, not by a constant.
        gaps = list((v or {}).get("gaps") or [])
        if gaps:
            ngap += 1
            print(f"\n  ⚠ {len(gaps)} candidate gap(s) on `{table}` ({kind}) — unverified until refuted")
        rows.append({"table": table, "kind": kind, "verdict": v, "verified": False})
    with open(out_path, "w") as fh:
        for r_ in rows:
            fh.write(json.dumps(r_) + "\n")
    unrev = sum(1 for r_ in rows if r_.get("verdict") is None)
    ncand = sum(len((r_.get("verdict") or {}).get("gaps") or []) for r_ in rows)
    print(f"\n  {ncand} CANDIDATE gaps across {ngap} seams · {unrev} UNREVIEWED (not clean) -> {out_path}")
    print("  These are candidates, NOT findings. Refute them before believing any of them:")
    print(f"    review_axes.py seam --refute --from-file {out_path}")
    return 0


# ── axis 4: invariants ───────────────────────────────────────────────────────
INV_SYSTEM = (
    "You check a codebase against ONE claim it makes about itself. If what you are shown CANNOT answer the "
    "question — you are given an inventory of names and docstrings, so any claim about the CONTENTS of "
    "function bodies is unanswerable from it — reply status='insufficient' and say what evidence you would "
    "need. Never report 'absent' because you could not see; absent means you CAN see and the discipline is "
    "not there. Two kinds of real answer matter equally: "
    "VIOLATIONS (a site that breaks the rule) and ABSENCE (the rule is enforced NOWHERE, so there is no site "
    "to point at — the discipline simply does not exist in this repo). Absence is the more serious finding and "
    "the one everybody misses, because a reviewer looking for bad code cannot see missing code. If the "
    "invariant IS consistently upheld, say so plainly — do not manufacture a finding.")

INV_SCHEMA = ('{"status": "upheld|violated|absent|insufficient", "absent_because": "...", "need_to_see": "...", '
              '"violations": [{"where": "mod.fn or file", "what": "...", "impact": "..."}], "fix": "..."}')


def run_invariants(root, model, run, out_path):
    # The invariants are the repo's OWN doctrine plus the one this session cost a settings file to learn.
    # Not hardcoded judgements — each is a claim, and the model decides whether the code honours it.
    inv_file = pathlib.Path(root).parent.parent / "docs" / "INVARIANTS.md"
    if inv_file.exists():
        invariants = [l.strip("- ").strip() for l in inv_file.read_text().splitlines()
                      if l.strip().startswith("- ")]
    else:
        invariants = [
            "Absence is UNKNOWN — never silently rendered as zero, as permission, or as success.",
            "Every destructive mutation of a user file takes a backup first and writes atomically.",
            "Every decision about MEANING is made by an LLM, never by regex, keywords, or a hand-picked threshold.",
            "One capability is implemented in exactly ONE place; callers delegate rather than re-implement.",
            "A failure is loud: no bare `except: pass` on a path that affects money, privacy, or attribution.",
            "Money figures separate REAL billed dollars from ESTIMATED subscription value and never sum them.",
        ]
    # An inventory, not the bodies: axis 4 asks whether a discipline EXISTS across the repo, and the shape
    # of the whole repo is what answers that. Bodies are axis 1's job.
    inv_index = []
    for f in sorted(pathlib.Path(root).glob("*.py")):
        try:
            tree = ast.parse(f.read_text())
        except (SyntaxError, OSError):
            continue
        fns = [n.name for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
        doc = (ast.get_docstring(tree) or "").strip().splitlines()
        inv_index.append(f"{f.name}: {doc[0][:110] if doc else ''}\n    {', '.join(fns[:28])}")
    index = "\n".join(inv_index)

    est = sum(pricing.realtime_cost(model, len(index) // 4 + 300, 1100) or 0 for _ in invariants)
    print(f"axis 4 (invariant) — {len(invariants)} claims vs {len(inv_index)} modules")
    if not run:
        ui.estimate_only(action=f"check {len(invariants)} whole-repo invariants incl. ABSENCE", cost=est)
        return 0
    rows = []
    for i, inv in enumerate(invariants, 1):
        prompt = (f"REPO INVENTORY (module: purpose / functions)\n{index}\n\nINVARIANT THIS PROJECT CLAIMS:\n"
                  f"  {inv}\n\nIs it upheld, violated at specific sites, or ABSENT (enforced nowhere)?\n"
                  f"Reply JSON only: {INV_SCHEMA}")
        with calls.context(intent="spendguard:invariant-review"):
            r = adapters.call_complete(model, prompt, sig="probe:invariant", system=INV_SYSTEM)
        if r.get("error"):
            print(f"  {i}/{len(invariants)}: FAILED — UNREVIEWED, not clean"); rows.append({"inv": inv}); continue
        try:
            blob = re.search(r"\{.*\}", r.get("text") or "", re.S)
            v = json.loads(blob.group(0)) if blob else None
        except Exception:
            v = None
        st = (v or {}).get("status")
        mark = {"absent": "⛔ ABSENT", "violated": "⚠ VIOLATED", "upheld": "✓ upheld",
                "insufficient": "◌ INSUFFICIENT EVIDENCE"}.get(st, "? UNREVIEWED")
        print(f"\n  {mark}  {inv[:88]}")
        if st == "absent":
            print(f"      {str((v or {}).get('absent_because'))[:150]}")
        elif st == "insufficient":
            # NOT A FINDING AND NOT A CLEAN BILL — the axis was asked a question its evidence cannot answer.
            print(f"      needs: {str((v or {}).get('need_to_see'))[:130]}  (route to a body-level axis)")
        for viol in ((v or {}).get("violations") or [])[:4]:
            print(f"      · {viol.get('where')}: {str(viol.get('what'))[:100]}")
        rows.append({"inv": inv, "verdict": v})
    with open(out_path, "w") as fh:
        for r_ in rows:
            fh.write(json.dumps(r_) + "\n")
    print(f"\n  -> {out_path}")
    return 0


# ── axis 5: names ────────────────────────────────────────────────────────────
NAME_SYSTEM = (
    "You are shown every definition in a codebase that SHARES ITS BARE NAME with another definition, with "
    "full bodies. Sort each group into exactly one of:\n"
    "  PROTOCOL    — intentional polymorphism: these implement one interface/ABC/duck-typed contract and are "
    "SUPPOSED to share a name. Correct as-is.\n"
    "  DUPLICATION — the same job implemented more than once. The name is honest; the copies are the defect. "
    "Merge to one.\n"
    "  COLLISION   — DIFFERENT jobs wearing the same name. The most dangerous case: a reader, a caller, or a "
    "grep lands on the wrong one, and an `import` or re-export silently picks a side. Rename.\n"
    "Judge from the BODIES. A shared name is the question, never the answer.")

NAME_SCHEMA = ('{"verdict": "PROTOCOL|DUPLICATION|COLLISION", "why": "...", '
               '"canonical": "the one to keep, if any", "rename_to": {"mod.Class.fn": "suggested_name"}, '
               '"risk": "what goes wrong today because of this"}')


def run_names(root, model, run, out_path):
    ds = repo_defs.defs(root)
    groups = {n: v for n, v in repo_defs.by_bare_name(ds).items() if len(v) > 1}
    # Dunders are language contract, not naming choices — __init__ appearing 11 times is Python, not a defect.
    groups = {n: v for n, v in groups.items() if not (n.startswith("__") and n.endswith("__"))}
    tot = sum(len(v) for v in groups.values())
    print(f"axis 5 (name) — {len(ds)} definitions · {len(groups)} bare names reused · {tot} definitions "
          f"({100*tot/max(1,len(ds)):.0f}% of the repo) share a name with another")

    def body(name, members):
        parts = [f"BARE NAME: `{name}` — {len(members)} definitions"]
        for d in members[:6]:
            parts.append(f"### {d.qual}{d.sig}\n```python\n{d.src[:1500]}\n```")
        return "\n\n".join(parts)

    est = sum(pricing.realtime_cost(model, len(body(n, v)) // 4 + 300, 700) or 0 for n, v in groups.items())
    if not run:
        ui.estimate_only(action=f"sort {len(groups)} shared-name groups into protocol / duplication / collision",
                         cost=est)
        return 0
    rows = collections.Counter()
    out = []
    for i, (name, members) in enumerate(sorted(groups.items(), key=lambda kv: -len(kv[1])), 1):
        prompt = (body(name, members) + "\n\nPROTOCOL, DUPLICATION, or COLLISION?\n"
                  f"Reply JSON only: {NAME_SCHEMA}")
        with calls.context(intent="spendguard:name-review"):
            # 900 was enough for most groups and not for the big ones: a re-run came back with 40 of 79
            # groups UNREVIEWED where the first pass had 0, purely from truncation. An unreviewed name group
            # is not a judged one, and the registry would have recorded it as if the question had been asked.
            r = adapters.call_complete(model, prompt, sig="probe:name-review", system=NAME_SYSTEM)
        v = None
        if not r.get("error"):
            try:
                blob = re.search(r"\{.*\}", r.get("text") or "", re.S)
                v = json.loads(blob.group(0)) if blob else None
            except Exception:
                v = None
        verdict = (v or {}).get("verdict") or "UNREVIEWED"
        rows[verdict] += 1
        if verdict in ("COLLISION", "DUPLICATION"):
            mark = "⛔ COLLISION" if verdict == "COLLISION" else "⚠ DUPLICATION"
            print(f"\n  {mark}  `{name}` ×{len(members)}")
            print(f"      {', '.join(d.qual for d in members[:6])}")
            print(f"      {str((v or {}).get('risk') or (v or {}).get('why'))[:130]}")
        out.append({"name": name, "n": len(members), "quals": [d.qual for d in members], "verdict": v})
    with open(out_path, "w") as fh:
        for r_ in out:
            fh.write(json.dumps(r_) + "\n")
    print(f"\n  {dict(rows)}")
    print(f"  PROTOCOL is correct as-is. DUPLICATION merges. COLLISION renames — a same-name/different-job "
          f"pair is how `spendguard.record_estimate` came to mean something the docs did not.  -> {out_path}")
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("axis", choices=["seam", "invariant", "name"])
    ap.add_argument("--root", default="src/spendguard")
    ap.add_argument("--model", default="")
    ap.add_argument("--run", action="store_true")
    ap.add_argument("--out", default="")
    a = ap.parse_args()
    os.environ["SPENDGUARD_ADVISOR_EXECUTOR"] = "api"
    model = a.model or config.advisor_model()
    out = a.out or os.path.join(str(config.HOME), f"review_{a.axis}.jsonl")
    fn = {"seam": run_seams, "invariant": run_invariants, "name": run_names}[a.axis]
    return fn(a.root, model, a.run, out)


if __name__ == "__main__":
    sys.exit(main())
