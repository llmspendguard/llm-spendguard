"""One ledger of every finding produced by every review pass, and what happened to it.

WHY. By 2026-08-11 this project had produced twenty-nine findings artifacts across four review waves, three
capability axes, a silent-failure triage, an unwired-capability scan and two refutation passes — roughly
2,700 rows. Nobody, including me, could say from memory which of those were CONFIRMED, which were FIXED, and
which were merely COUNTED. Twice I reported a number to the user that turned out to be a different quantity
than I claimed: "9 survived refutation" was really 2 survived + 7 unverified, and "377 seam gaps" was the
output of a confidence filter over a truncation-damaged map. Both times the raw file had the right answer
sitting in it.

So the ledger is generated, never remembered. It reads every artifact, sorts each row into one of four
states, and — this is the point — refuses to let the states blur:

  CONFIRMED+GUARDED   a defect that survived validation AND has a regression guard naming it. Done.
  CONFIRMED           survived validation, no guard found. NOT done: a fix without a guard comes back.
  UNVERIFIED          the check could not be completed (a failed call, a truncated reply, no refuter
                      answered). This is the state that keeps getting silently rounded into one of the
                      others, in both directions, and it is neither.
  REFUTED             a reviewer's claim that did not survive scrutiny. Correctly closed, worth counting so
                      the review's precision is visible rather than assumed.

The GUARD cross-reference is mechanical on purpose: a guard is present when a test names the module and the
symbol. That can under-report (a guard phrased differently), so it is reported as a floor, never as
"everything else is unguarded" — the same absence-is-unknown rule the guards themselves enforce.

CLI: finding_ledger.py [--dir ~/.spendguard] [--open]     # zero spend, reads files only
"""
import argparse
import collections
import json
import os
import pathlib
import sys

# Each artifact says where its rows came from and how a row declares itself confirmed. Kept as data so a new
# review pass is one line here rather than a new branch in the reader.
SOURCES = [
    ("validated_findings.jsonl",       "wave 1 (validated)",        "verdict"),
    ("validated_wave1_grouped.jsonl",  "wave 1 (grouped sites)",    "verdict"),
    ("validated_wave2.jsonl",          "wave 2 (validated)",        "verdict"),
    ("validated_wave2_grouped.jsonl",  "wave 2 (grouped sites)",    "verdict"),
    ("validated_wave3.jsonl",          "wave 3 (validated)",        "verdict"),
    ("repo_review_wave4.jsonl",        "wave 4 (RAW, unvalidated)", "raw"),
    ("capability_drift.jsonl",         "axis 2 — concept drift",    "drift"),
    ("review_invariant.jsonl",         "axis 4 — invariants",       "invariant"),
    ("review_name.jsonl",              "axis 5 — names",            "name"),
    ("silent_failures.jsonl",          "silent-failure triage",     "silent"),
    ("silent_failures_verified.jsonl", "  └ after refutation",      "survived"),
    ("silent_reverified.jsonl",        "  └ after re-refutation",   "survived"),
    ("unwired.jsonl",                  "unwired capabilities",      "unwired"),
    ("wave_resolution.jsonl",          "  └ wave rows resolved",    "resolution"),
]


def verdict_of(row, kind):
    """(verdict_as_the_model_recorded_it, label) — READ, never re-interpreted.

    An earlier version of this mapped each artifact's verdict onto four states of my own — "misleading" and
    "dangerous" both becoming CONFIRMED, "PROTOCOL" becoming REFUTED, and so on. Reading a closed enum that
    the triage prompt itself defined is parsing, but CHOOSING that `misleading` counts as a confirmed defect
    is an editorial judgement I was making silently and then presenting as a count. That is the same move
    that turned "2 survived, 7 unverified" into a reported "9 survived": a collapse, performed by me, in the
    summary rather than in the data.

    So nothing is collapsed. Each row reports the verdict its own pass assigned, the tally is per distinct
    verdict, and a reader sees `dangerous 60 · misleading 72 · benign 43` rather than a number I decided the
    meaning of. Missing or unparseable stays UNJUDGED — a state, not a rounding.
    """
    if kind == "raw":
        return "UNJUDGED(raw)", row.get("title") or row.get("summary") or "?"
    if kind == "verdict":
        # The wave artifacts record the validators' conclusion in `outcome` ("CONFIRMED" / "rejected" /
        # "SPLIT — needs a human") with the per-vendor reasoning under `verdicts`. The first version of this
        # reader looked for a `verdict.is_real` key that does not exist in these files, so every row came
        # back UNJUDGED and three whole waves — 34 confirmed defects that WERE fixed — reported as zero.
        # A reader that silently finds nothing looks exactly like a corpus with nothing in it, which is the
        # same failure this ledger was built to stop, committed by the ledger.
        label = f"{row.get('file', '?')}:{row.get('line', '?')} {(row.get('claims') or [''])[0]}"
        return (row.get("outcome") or "UNJUDGED", label)
    if kind == "drift":
        v = row.get("verdict") or {}
        if not v:
            return "UNJUDGED", row.get("capability", "?")
        bad = [d for d in (v.get("divergences") or []) if d.get("wrong")]
        if not v.get("same_job"):
            return "different_jobs", row.get("capability", "?")
        return ("drifted_wrong_copy" if (v.get("drifted") and bad) else "same_job_no_wrong_divergence",
                row.get("capability", "?"))
    if kind == "invariant":
        return ((row.get("verdict") or {}).get("status") or "UNJUDGED", (row.get("inv") or "?")[:70])
    if kind == "name":
        return ((row.get("verdict") or {}).get("verdict") or "UNJUDGED",
                f"{row.get('name')} x{row.get('n')}")
    if kind == "silent":
        return ((row.get("verdict") or {}).get("verdict") or "UNJUDGED", row.get("where", "?"))
    if kind == "survived":
        s = row.get("survived")
        # THREE STATES STAY THREE. None means the refuters never answered; it is not False.
        return ({True: "survived", False: "refuted"}.get(s, "UNVERIFIED"), row.get("where", "?"))
    if kind == "unwired":
        return ((row.get("verdict") or {}).get("verdict") or "UNJUDGED", row.get("qual", "?"))
    if kind == "resolution":
        # The pass that closed out the rows this ledger had to report as UNKNOWN. Two question types, so
        # two verdict vocabularies, kept apart rather than merged into one score: a guard question answers
        # "is it protected", a split question answers "is it real". Folding them would hide which was asked.
        v = row.get("verdict")
        if not v:
            return "UNJUDGED", row.get("where", "?")
        if row.get("kind") == "guard":
            return ("guarded" if v.get("guarded") else "NEEDS_GUARD"), row.get("where", "?")
        return (v.get("verdict") or "UNJUDGED"), row.get("where", "?")
    return "UNJUDGED", "?"


# The ONE cross-cutting fact worth computing here, and it is not a judgement: which verdicts leave work to
# do. Declared per artifact by the pass that produced it, so it is that pass's own definition of a finding
# rather than mine — and it is DATA, visible and editable, not a decision buried in a branch.
ACTIONABLE = {
    "CONFIRMED", "SPLIT — needs a human", "drifted_wrong_copy", "absent", "violated",
    "COLLISION", "DUPLICATION", "dangerous", "misleading", "survived", "UNWIRED",
    "NEEDS_GUARD", "real_now",          # from the resolution pass: unprotected, and still-real
}


def guard_text(repo):
    """Everything the test suite asserts, as one blob — the corpus a guard reference is looked up in."""
    parts = []
    for t in sorted((pathlib.Path(repo) / "tests").glob("*.py")):
        try:
            parts.append(t.read_text(errors="ignore"))
        except OSError:
            continue
    return "\n".join(parts)


def guarded(label, guards):
    """True / False / None — is there a test naming this thing?

    None means THE QUESTION COULD NOT BE ANSWERED MECHANICALLY, and it is a third answer on purpose. The
    wave artifacts label a finding `budget.py:310 <claim text>`; matching that to a guard means deciding
    whether a test's prose describes the same defect, which is a judgement about meaning and not something a
    token comparison can do. The first version of this returned False for those and printed "0 guarded"
    across three whole waves — 66 findings that WERE fixed and WERE guarded, reported as unguarded, by a
    matcher that had simply failed to run. Absence of a match is not absence of a guard.
    """
    tok = (label or "").split()[0].strip("`'\"")
    if not tok:
        return None
    if tok.endswith(".py") or ".py:" in tok:
        return None                          # file:line — needs a judgement, see docstring
    if "." in tok:
        mod, _, sym = tok.partition(".")
        sym = sym.split(":")[0]
        return (sym in guards) and (mod in guards)
    return tok in guards


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default=str(pathlib.Path.home() / ".spendguard"))
    ap.add_argument("--repo", default=os.getcwd())
    ap.add_argument("--open", action="store_true", help="list the CONFIRMED-but-unguarded rows")
    a = ap.parse_args()
    guards = guard_text(a.repo)
    d = pathlib.Path(a.dir)

    grand = collections.Counter()
    open_rows = []
    n_act = n_guard = 0
    print(f"  {'source':<28}{'rows':>6}{'actionable':>12}{'guarded':>9}   verdicts as each pass recorded them")
    print("  " + "─" * 92)
    for fname, label, kind in SOURCES:
        p = d / fname
        if not p.exists():
            print(f"  {label:<28}{'— artifact absent —':>27}")
            continue
        c = collections.Counter()
        act = g = 0
        for line in p.open():
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except Exception:
                c["UNPARSEABLE"] += 1
                continue
            v, lab = verdict_of(row, kind)
            c[v] += 1
            grand[v] += 1
            if v in ACTIONABLE:
                act += 1
                gd = guarded(lab, guards)
                if gd is True:
                    g += 1
                elif gd is False:
                    open_rows.append((label, lab))
                else:
                    c["_guard_unknown"] += 1
                    grand["GUARD_UNKNOWN"] += 1
        n_act += act
        n_guard += g
        mix = " · ".join(f"{k} {n}" for k, n in c.most_common(4) if not k.startswith("_"))
        gcol = f"{g}" if not c["_guard_unknown"] else f"{g}+{c['_guard_unknown']}?"
        print(f"  {label:<28}{sum(c.values()) - c['_guard_unknown']:>6}{act:>12}{gcol:>9}   {mix[:44]}")

    print("  " + "─" * 92)
    print(f"  {'TOTAL':<28}{sum(grand.values()):>6}{n_act:>12}{n_guard:>9}")
    print()
    unk = grand["GUARD_UNKNOWN"]
    print(f"  ACTIONABLE findings          {n_act}")
    print(f"    ├ a guard names it         {n_guard}")
    print(f"    ├ NO guard found           {n_act - n_guard - unk}   ← open; a fix without a guard comes back")
    # THE RESOLUTION PASS ANSWERED THESE, so reporting them as unknown would be reporting a state the
    # ledger's own input has since left. `resolved` is what that pass settled; whatever it did not reach
    # stays unknown, because a partial answer is not a complete one.
    resolved = grand["NEEDS_GUARD"] + grand["guarded"] + grand["already_fixed"] + \
        grand["not_a_defect"] + grand["real_now"]
    still_unknown = max(0, unk - resolved)
    print(f"    ├ resolved by the wave pass {resolved}   ({grand['NEEDS_GUARD']} need a guard · "
          f"{grand['real_now']} STILL REAL · {grand['already_fixed']} already fixed · "
          f"{grand['not_a_defect']} not a defect · {grand['guarded']} already guarded)")
    print(f"    └ guard status still UNKNOWN {still_unknown}")
    unv = grand["UNVERIFIED"] + grand["UNJUDGED"] + grand["UNJUDGED(raw)"] + grand["UNPARSEABLE"]
    print(f"  UNVERIFIED / UNJUDGED        {unv}   ← the check did not complete. Neither a finding nor a "
          f"clean bill.")
    print("\n  Verdicts are printed as each pass recorded them, not remapped into categories of mine — "
          "the collapse is where '2 survived + 7 unverified' became a reported '9 survived'.")
    print("  The guard column is a FLOOR: it matches a test naming both module and symbol, so a guard "
          "written differently reads as absent. It under-counts, never over-counts.")
    if a.open:
        print(f"\n  ACTIONABLE WITHOUT A GUARD ({len(open_rows)}):")
        for src, lab in open_rows[:60]:
            print(f"    [{src.strip()[:26]:<26}] {lab[:84]}")
        if len(open_rows) > 60:
            print(f"    … +{len(open_rows) - 60} more")
    return 0


if __name__ == "__main__":
    sys.exit(main())
