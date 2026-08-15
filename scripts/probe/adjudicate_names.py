"""Self-contained agentic NAME-axis adjudicator — a MODEL reads the colliding function BODIES and decides
PROTOCOL / DUPLICATION / COLLISION, writing <home>/review_name.jsonl for promote_name_verdicts.py.

WHY THIS EXISTS (and why it dogfoods spendguard.ask). tests/test_names_stay_unique.py requires every colliding
bare name to carry a recorded verdict, and it is explicit that the classification is a JUDGEMENT for a model, not
a rule about names — so it must not be hand-asserted. The sanctioned producer of that verdict was
`honestreview name`, an EXTERNAL repo. But this package ships `dependencies = []` and its suite must not need a
private reviewer to pass. So the adjudication is done here with spendguard's OWN cross-LLM surface
(`spendguard.ask`) — which makes the package self-contained AND exercises the ask surface end to end, under the
gate, on the $0 lane, as a real task rather than a toy.

    .venv.nosync/bin/python scripts/probe/adjudicate_names.py                 # judge every UNJUDGED collision
    .venv.nosync/bin/python scripts/probe/adjudicate_names.py acquire release # judge only these
then:
    .venv.nosync/bin/python scripts/probe/promote_name_verdicts.py <names…>   # transcribe into NAME_REGISTRY.json

The classification is agentic (meaning → LLM). Extracting the bodies is pure AST (parsing a fixed shape), which
is the one part where code is correct.
"""
import argparse
import ast
import collections
import json
import os
import pathlib
import sys

REPO = pathlib.Path(__file__).resolve().parents[2]
SRC = REPO / "src" / "spendguard"
sys.path.insert(0, str(REPO / "src"))

import spendguard                                        # gate: fail closed if this interpreter is not enforcing
spendguard.require()
os.environ.setdefault("SPENDGUARD_ADVISOR_EXECUTOR", "pool")   # the judge rides the $0 Claude lane when available
from spendguard import config                            # noqa: E402

# The judge: one strong model. A name verdict is a careful reading of intent — not a place to skimp on the model.
JUDGE = os.environ.get("SPENDGUARD_NAME_JUDGE", "anthropic:claude-opus-4-8")

VERDICT_SCHEMA = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": ["PROTOCOL", "DUPLICATION", "COLLISION"]},
        "why": {"type": "string"},
    },
    "required": ["verdict", "why"],
}

SYSTEM = (
    "You adjudicate whether functions that share one bare NAME across a Python codebase are a legitimate "
    "protocol or a defect. Decide from the BODIES, not the names. Exactly one verdict:\n"
    "  PROTOCOL   — a uniform contract deliberately implemented/dispatched by name: N providers implementing the "
    "same interface method, or a module-level facade delegating to a same-named method on a class. Correct "
    "polymorphism; must NOT be renamed.\n"
    "  DUPLICATION— two implementations of the SAME job that can drift apart and give different answers in "
    "different callers. Should be merged, not kept.\n"
    "  COLLISION  — the same bare name on DIFFERENT jobs, so a grep/import/re-export can silently pick the wrong "
    "one. Should be renamed to something unique.\n"
    "Be strict and concrete. `why` is one or two sentences naming the actual jobs."
)


def _collisions():
    """{bare_name: [(qualname, source_text)]} for every function that shares its bare name with another under
    src/spendguard — the same identity walk the test does, but capturing the body for the judge."""
    by_bare = collections.defaultdict(list)
    for f in sorted(SRC.rglob("*.py")):
        text = f.read_text(errors="ignore")
        try:
            tree = ast.parse(text)
        except SyntaxError:
            continue

        def walk(node, scope):
            for ch in ast.iter_child_nodes(node):
                if isinstance(ch, ast.ClassDef):
                    walk(ch, scope + [ch.name])
                elif isinstance(ch, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    qual = ".".join([f.stem] + scope + [ch.name])
                    seg = ast.get_source_segment(text, ch) or ""
                    by_bare[ch.name].append((qual, seg))
                    walk(ch, scope + [ch.name])

        walk(tree, [])
    return {n: v for n, v in by_bare.items()
            if len(v) > 1 and not (n.startswith("__") and n.endswith("__"))}


def _prompt(name, members):
    parts = [f"The bare function name `{name}` is defined {len(members)} times under src/spendguard. "
             f"Classify the group. Here are the definitions:\n"]
    for qual, seg in members:
        body = seg if len(seg) <= 2600 else seg[:2600] + "\n    # … (truncated)"
        parts.append(f"# ── {qual} ──\n{body}\n")
    parts.append('\nReply as JSON: {"verdict": "PROTOCOL|DUPLICATION|COLLISION", "why": "<1-2 sentences>"}.')
    return "\n".join(parts)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("names", nargs="*", help="bare names to judge (default: every UNJUDGED collision)")
    ap.add_argument("--budget", type=float, default=1.0, help="hard $ ceiling per judge call (metered fallback)")
    a = ap.parse_args(argv)

    groups = _collisions()
    registry = json.loads((REPO / "docs" / "NAME_REGISTRY.json").read_text())["names"]
    if a.names:
        targets = [n for n in a.names if n in groups]
        missing = [n for n in a.names if n not in groups]
        if missing:
            print(f"note: not colliding (nothing to judge): {missing}")
    else:
        targets = sorted(set(groups) - set(registry))          # every unjudged collision
    if not targets:
        print("no unjudged collisions — nothing to do.")
        return 0

    out_path = pathlib.Path(config.HOME) / "review_name.jsonl"
    rows = []
    print(f"adjudicating {len(targets)} name group(s) with {JUDGE} (executor={os.environ.get('SPENDGUARD_ADVISOR_EXECUTOR')}):\n")
    for name in targets:
        members = groups[name]
        r = spendguard.ask(_prompt(name, members), vendors=[JUDGE], schema=VERDICT_SCHEMA,
                           system=SYSTEM, purpose="adjudicate:names", deadline_s=150, budget_usd=a.budget)
        verdict = None
        if r.complete and r.answers:
            try:
                d = json.loads(r.answers[0])
                verdict = {"verdict": d["verdict"], "why": d.get("why") or ""}
            except Exception as e:
                print(f"  {name:12} UNPARSEABLE judge reply ({e}) — recorded as undecided")
        else:
            print(f"  {name:12} judge did not answer ({r.by_vendor}) — recorded as undecided")
        rows.append({"name": name, "n": len(members), "verdict": verdict})
        if verdict:
            print(f"  {name:12} {verdict['verdict']:11} — {verdict['why'][:96]}")

    out_path.write_text("\n".join(json.dumps(x) for x in rows) + "\n")
    decided = [x["name"] for x in rows if x["verdict"]]
    print(f"\nwrote {len(rows)} row(s) → {out_path}")
    print(f"decided: {decided}")
    print(f"next: .venv.nosync/bin/python scripts/probe/promote_name_verdicts.py {' '.join(decided)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
