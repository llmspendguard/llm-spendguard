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
import ast, json, pathlib, sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts" / "probe"))
import repo_defs                                                          # noqa: E402

REGISTRY = json.loads((ROOT / "docs" / "NAME_REGISTRY.json").read_text())["names"]
ds = repo_defs.defs(ROOT / "src" / "spendguard")
assert ds, "no definitions found — the extractor is broken, which would make this test vacuously pass"

groups = {n: v for n, v in repo_defs.by_bare_name(ds).items()
          if len(v) > 1 and not (n.startswith("__") and n.endswith("__"))}
unjudged = sorted(set(groups) - set(REGISTRY))
assert not unjudged, (
    f"{len(unjudged)} function name(s) now collide and have NOT been judged: {unjudged}\n"
    f"  A shared name is not automatically wrong — it may be a protocol. But it must be DECIDED, not\n"
    f"  assumed. Run:  .venv/bin/python scripts/probe/review_axes.py name --run\n"
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
    f"  Re-run:  .venv/bin/python scripts/probe/review_axes.py name --run")

# The registry must shrink, never quietly grow: a name that stopped colliding cannot come back unnoticed.
stale = sorted(set(REGISTRY) - set(groups))
if stale:
    print(f"  note: {len(stale)} registered name(s) no longer collide — remove them from the registry: {stale[:6]}")

worst = [n for n, v in REGISTRY.items() if v.get("verdict") == "COLLISION" and n in groups]
print(f"ok — {len(groups)} colliding names, all judged. {len(worst)} still classed COLLISION (different jobs, "
      f"one name) and awaiting rename.")
