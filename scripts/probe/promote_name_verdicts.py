"""Record adjudicated NAME-axis verdicts into the frozen docs/NAME_REGISTRY.json.

The workflow that tests/test_names_stay_unique.py documents has two halves:
  1. `honestreview name src/spendguard --run`  — a MODEL reads the colliding bodies and decides
     PROTOCOL / DUPLICATION / COLLISION, writing one row per group to ~/.spendguard/review_name.jsonl.
  2. record that verdict in docs/NAME_REGISTRY.json  — which is what this script does, so the "record it"
     step is a command you RUN, not a 420-line JSON you hand-edit (hand-editing is how a wrong `n` or a typo'd
     verdict lands silently).

This script does NOT decide anything — the decision already happened in step 1. It only TRANSCRIBES the named
groups' verdicts into the registry, verbatim `why` (no truncation — losing the reasoning is the shortcut the
existing truncated entries already took), preserving alphabetical order and the file's header. It promotes ONLY
the names passed on argv, so re-running it never churns the other frozen verdicts.

    python scripts/probe/promote_name_verdicts.py _recording stream
    python scripts/probe/promote_name_verdicts.py --from <jsonl> _recording stream
    python scripts/probe/promote_name_verdicts.py --remove record_estimate register   # a name that stopped colliding

Removing a name that STILL collides is caught immediately: test_names_stay_unique re-flags it as unjudged, so
the test is the safety net for --remove. The registry is meant to SHRINK — a fixed collision should leave it.
"""
import argparse
import json
import pathlib
import sys

REPO = pathlib.Path(__file__).resolve().parents[2]
REGISTRY_PATH = REPO / "docs" / "NAME_REGISTRY.json"


def _default_findings():
    """The adjudicator writes to <spendguard-home>/review_name.jsonl — resolve it, never hardcode ~/.spendguard."""
    sys.path.insert(0, str(REPO / "src"))
    from spendguard import config
    return pathlib.Path(config.HOME) / "review_name.jsonl"


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("names", nargs="*", help="bare names to promote from the findings into the registry")
    ap.add_argument("--from", dest="findings", default=None,
                    help="the review_name.jsonl produced by `honestreview name --run` (default: spendguard home)")
    ap.add_argument("--remove", nargs="+", default=[], metavar="NAME",
                    help="drop these names from the registry — for a name that no longer collides (shrinks the file)")
    args = ap.parse_args(argv)
    if not args.names and not args.remove:
        ap.error("give at least one name to promote, or --remove NAME ... to prune")

    reg = json.loads(REGISTRY_PATH.read_text())
    names = reg["names"]

    # promotions — load the adjudicator's findings only when there is something to promote
    changed = []
    if args.names:
        findings_path = pathlib.Path(args.findings) if args.findings else _default_findings()
        if not findings_path.exists():
            sys.exit(f"no findings at {findings_path} — run `honestreview name src/spendguard --run` first")
        # index rows by name; a row with a null verdict was truncated/errored and is NOT a decision
        judged = {}
        for line in findings_path.read_text().splitlines():
            if not line.strip():
                continue
            r = json.loads(line)
            v = r.get("verdict")
            if v and v.get("verdict"):
                judged[r["name"]] = r
        missing = [n for n in args.names if n not in judged]
        if missing:
            sys.exit(f"these names carry no judged verdict in {findings_path.name}: {missing}\n"
                     f"  (a group the adjudicator left UNREVIEWED must be re-run, not recorded as if decided)")
        for n in args.names:
            v = judged[n]["verdict"]
            entry = {"n": judged[n]["n"], "verdict": v["verdict"], "why": v.get("why") or ""}
            was = names.get(n)
            names[n] = entry
            changed.append(("updated" if was else "added", n, entry))

    # removals — a name that stopped colliding leaves the registry (shrinking it is the goal)
    gone = [n for n in args.remove if names.pop(n, None) is not None]
    absent = [n for n in args.remove if n not in gone]

    reg["names"] = dict(sorted(names.items()))                 # keep the file alphabetical, like every other entry
    REGISTRY_PATH.write_text(json.dumps(reg, indent=2, ensure_ascii=False) + "\n")

    for verb, n, entry in changed:
        print(f"  {verb:8} `{n}`  n={entry['n']}  {entry['verdict']}")
        print(f"           {entry['why'][:110]}")
    for n in gone:
        print(f"  removed  `{n}`")
    if absent:
        print(f"  (not in registry, nothing to remove: {absent})")
    print(f"\n[OK] {len(changed)} recorded, {len(gone)} pruned → {REGISTRY_PATH.relative_to(REPO)}")


if __name__ == "__main__":
    main()
