"""Domain-seam symbol SSOT: the names a domain's modules expose to EXTERNAL cyclic consumers.

For a given domain (context id), this prints, per home module that currently receives an inbound
cross-domain cyclic import edge, the exact set of names that external strongly-connected-component
modules import from it -- classified into exception / class / constant / callable. This is the
authoritative input to a dependency-inversion "domain seam": the callables forward via a resolver,
the plain-dep-free constants and the refusal exceptions move into a dep-free sink, and every such
inbound edge leaves the import cycle once its consumers are repointed off the home.

Resolution mirrors spendguard.analyze.assurance.architectural_fitness.collect_architectural_import_edges
(importlib.util.resolve_name + legacy-shim collapse), and additionally follows attribute access on a
module bound by `import home as g` / `from pkg import home` so module-alias reach-ins are not missed.

Usage:
    python scripts/probe/domain_seam_symbol_ssot.py <domain-context-id>
    python scripts/probe/domain_seam_symbol_ssot.py --list        # list all domain context ids

$0, read-only. Root is derived from this file's location -- nothing is hardcoded to a machine.
"""
import ast
import importlib
import importlib.util
import sys
import types
from collections import defaultdict
from pathlib import Path

from spendguard.analyze.assurance import architectural_fitness as af
from spendguard.analyze.assurance import import_behavior_snapshot as ibs

REPO_ROOT = Path(__file__).resolve().parents[2]


def _short(module_name):
    return module_name.replace("spendguard.", "")


def _load_graph():
    settings = af.load_architectural_fitness_baseline(REPO_ROOT)
    registry = af.load_architectural_context_registry(REPO_ROOT)
    shim_map = {
        declaration["legacy_module"]: declaration["current_module"]
        for declaration in registry["domain_legacy_path_shims"]
    }
    units = af.collect_architectural_source_units(REPO_ROOT, settings)
    edges = af.collapse_architectural_shim_edges(
        af.collect_architectural_import_edges(units, legacy_modules=shim_map), shim_map
    )
    recorded = {tuple(edge) for edge in settings["recorded_cyclic_edges"]}
    ratchet = af.measure_architectural_cyclic_edge_ratchet(edges, recorded, shim_map)
    cyclic = ratchet["magnitude"]["cyclic_edges"]
    scc = set(max(ratchet["magnitude"]["components"], key=len))
    module_to_context = {}
    for context in registry["contexts"]:
        for backing in context["backing_modules"]:
            module_to_context["spendguard." + backing] = context["id"]
    eager = set(
        ibs.load_import_behavior_snapshot(REPO_ROOT)["entry_points"]["package_import"]["executed_modules"]
    )
    return units, cyclic, scc, module_to_context, shim_map, eager, registry


def _names_referenced(consumer_module, home_module, units, shim_map):
    """Names of home_module that consumer_module references (from-imports + attr access on a module alias)."""
    path, tree = units[consumer_module]
    package = consumer_module if path.stem == "__init__" else consumer_module.rpartition(".")[0]
    collapse = lambda name: shim_map.get(name, name)
    direct_names = set()
    module_alias_names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            base = (
                importlib.util.resolve_name("." * node.level + (node.module or ""), package)
                if node.level
                else node.module
            )
            if not base:
                continue
            if collapse(base) == home_module:
                for alias in node.names:
                    submodule = base + "." + alias.name
                    if collapse(submodule) == home_module or submodule in units:
                        continue
                    direct_names.add(alias.name)
            else:
                for alias in node.names:
                    if collapse(base + "." + alias.name) == home_module:
                        module_alias_names.add(alias.asname or alias.name)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if collapse(alias.name) == home_module:
                    module_alias_names.add(alias.asname or alias.name.split(".")[0])
    attribute_names = set()
    if module_alias_names:
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Attribute)
                and isinstance(node.value, ast.Name)
                and node.value.id in module_alias_names
            ):
                attribute_names.add(node.attr)
    return direct_names | attribute_names


def _classify(home_module, name):
    try:
        value = getattr(importlib.import_module(home_module), name)
    except Exception as error:  # noqa: BLE001 - classification probe, never fatal
        return "MISSING"
    if isinstance(value, type):
        return "EXC" if issubclass(value, BaseException) else "CLASS"
    if isinstance(value, types.ModuleType):
        return "SUBMOD"
    if callable(value):
        return "CALLABLE"
    return "CONST"


def main(argv):
    units, cyclic, scc, module_to_context, shim_map, eager, registry = _load_graph()
    if not argv or argv[0] in {"--list", "-l"}:
        for context in registry["contexts"]:
            print(context["id"])
        return 0
    domain = argv[0]
    domain_modules = {module for module, context in module_to_context.items() if context == domain}
    if not domain_modules:
        print(f"no modules found for domain context id {domain!r}; use --list", file=sys.stderr)
        return 2
    context_of = lambda module: module_to_context.get(module, "UNASSIGNED")
    inbound = defaultdict(set)
    for source, target in cyclic:
        if source in scc and target in domain_modules and context_of(source) != domain:
            inbound[target].add(source)
    print(f"domain={domain}  SCC={len(scc)}  cyclic={len(cyclic)}  inbound homes={len(inbound)}\n")
    total = 0
    for home in sorted(inbound, key=lambda module: -len(inbound[module])):
        references = set()
        for consumer in inbound[home]:
            references |= _names_referenced(consumer, home, units, shim_map)
        buckets = defaultdict(list)
        for name in sorted(references):
            buckets[_classify(home, name)].append(name)
        total += len(inbound[home])
        eager_label = "EAGER" if home in eager else "LAZY"
        print(
            f"[{_short(home)}]  {eager_label}  {len(inbound[home])} inbound edge(s); "
            f"{len(references)} name(s) imported externally:"
        )
        for kind in ("EXC", "CLASS", "CONST", "CALLABLE", "SUBMOD", "MISSING"):
            if buckets[kind]:
                print(f"   {kind:<9}({len(buckets[kind]):>2}) {', '.join(buckets[kind])}")
        print()
    print(f"TOTAL inbound cross-domain cyclic edges into [{domain}]: {total}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
