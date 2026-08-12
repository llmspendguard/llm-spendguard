"""A NEW function name colliding with an existing one must be judged before it lands.

WHY. 81 bare names are shared by 265 definitions here (23% of the repo). Some of that is correct — five
providers implementing `Source.truth_total` is polymorphism working. Some of it is a live trap:
`spendguard.record_estimate` (bulkgate, writes gate_ledger, authorizes spend) and `calibrate.record_estimate`
(writes cost_predictions, trains the estimator) are different jobs wearing one name, docs point consumers at
the second, and the top-level re-export binds the first.

This test does NOT decide which kind a collision is — that is a judgement about intent and it belongs to a
model (`review_axes.py name --run`, verdicts frozen in docs/NAME_REGISTRY.json). What it enforces is that no
UNJUDGED collision appears: finding that two definitions share a name is pure identity, fixed by the AST, so
code is the right tool for exactly that part and no further.
"""
import collections, json, pathlib, sys

ROOT = pathlib.Path(__file__).resolve().parent.parent

import ast


def _defs(root):
    """(bare_name, module.Class.method) for every function under root — stdlib only, ON PURPOSE.

    A BETTER EXTRACTOR EXISTS AND IS DELIBERATELY NOT USED HERE. symgrep does this with tree-sitter and an
    mtime cache, and an earlier version of this test imported it. That made llm-spendguard's suite
    unrunnable for anyone without symgrep — and this package is PUBLIC, ships `dependencies = []` on
    purpose, and installs into other people's environments. A public repo whose tests need a private
    dependency is broken for every external contributor, which is a far worse defect than the twelve lines
    below.

    This is not the duplication rule being waived, it is the rule being applied. That rule targets two
    implementations of a JOB that can drift apart and produce different answers in different callers. This
    walk has exactly one consumer — the assertions in this file — and computes exactly one thing that is
    checked against a frozen registry three lines later. It is a fixture, not infrastructure. The thing that
    WAS infrastructure (an 89-line general-purpose extractor imported by six modules) is correctly gone.

    Scope-qualified, because a flat `module.name` key collides and would attribute one body to another name.
    """
    out = []
    for f in sorted(pathlib.Path(root).rglob("*.py")):
        try:
            tree = ast.parse(f.read_text(errors="ignore"))
        except SyntaxError:
            continue                      # unparseable: reported by the assert below via a short inventory
        def walk(node, scope):
            for ch in ast.iter_child_nodes(node):
                if isinstance(ch, ast.ClassDef):
                    walk(ch, scope + [ch.name])
                elif isinstance(ch, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    out.append((ch.name, ".".join([f.stem] + scope + [ch.name])))
                    walk(ch, scope + [ch.name])
        walk(tree, [])
    return out


REGISTRY = json.loads((ROOT / "docs" / "NAME_REGISTRY.json").read_text())["names"]
ds = _defs(ROOT / "src" / "spendguard")
assert ds, "no definitions found — the extractor is broken, which would make this test vacuously pass"

_by_bare = collections.defaultdict(list)
for _bare, _qual in ds:
    _by_bare[_bare].append(_qual)
groups = {n: v for n, v in _by_bare.items()
          if len(v) > 1 and not (n.startswith("__") and n.endswith("__"))}
unjudged = sorted(set(groups) - set(REGISTRY))
assert not unjudged, (
    f"{len(unjudged)} function name(s) now collide and have NOT been judged: {unjudged}\n"
    f"  A shared name is not automatically wrong — it may be a protocol. But it must be DECIDED, not\n"
    f"  assumed. Run:  .venv/bin/python honestreview name src/spendguard --run\n"
    f"  then record the verdict in docs/NAME_REGISTRY.json. Renaming to a unique name also clears this.")

# A GROUP THAT GROWS IS ALSO UNJUDGED. The first cut of this test only compared the SET of names, so adding a
# third `_windows` alongside the two already registered passed clean — the negative control caught it. The
# judgement in the registry was made about N specific bodies; an N+1th body was never read by anything.
grown = sorted((n, REGISTRY[n]["n"], len(groups[n])) for n in set(groups) & set(REGISTRY)
               if len(groups[n]) > REGISTRY[n]["n"])
assert not grown, (
    f"{len(grown)} name group(s) gained a definition that no verdict covers: "
    + ", ".join(f"`{n}` was {was} now {now}" for n, was, now in grown) + "\n"
    f"  The registered verdict was reached by reading the ORIGINAL bodies; the new one has not been read.\n"
    f"  Re-run:  .venv/bin/python honestreview name src/spendguard --run")

# The registry must shrink, never quietly grow: a name that stopped colliding cannot come back unnoticed.
stale = sorted(set(REGISTRY) - set(groups))
if stale:
    print(f"  note: {len(stale)} registered name(s) no longer collide — remove them from the registry: {stale[:6]}")

worst = [n for n, v in REGISTRY.items() if v.get("verdict") == "COLLISION" and n in groups]
print(f"ok — {len(groups)} colliding names, all judged. {len(worst)} still classed COLLISION (different jobs, "
      f"one name) and awaiting rename.")
