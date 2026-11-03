"""Offline architectural fitness functions; measured bars and scope live in data."""

import ast
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

from . import config
from .capability_readiness import CapabilityReadinessEvidence

ARCHITECTURAL_BASELINE_RESOURCE = "architectural_fitness_baseline.json"
ARCHITECTURAL_PACKAGE_RELATIVE_ROOT = Path("src") / "spendguard"
ARCHITECTURAL_LAYER_CONFIGURATION_FILENAME = "architecture.ini"
ARCHITECTURAL_COMPLEXITY_REVIEW_KEY_PREFIX = "reviewed_"
ARCHITECTURAL_LARGEST_SCC_CEILING_KEY = "max_largest_scc_size"
ARCHITECTURAL_CYCLIC_EDGE_CEILING_KEY = "max_cyclic_edge_count"
ARCHITECTURAL_LEGACY_SHIM_COUNT_CEILING_KEY = "max_legacy_shim_count"
ARCHITECTURAL_DYNAMIC_IMPORT_BASELINE_KEY = "recorded_dynamic_spendguard_imports"
ARCHITECTURAL_BOUNDARY_BASELINE_KEY = "recorded_cross_context_nonsurface_imports"
ARCHITECTURAL_LEGACY_FLAT_MODULE_BASELINE_KEY = "recorded_legacy_flat_modules"
ARCHITECTURAL_BOUNDARY_SHIM_KEY = "boundary_exempt_shim_modules"
ARCHITECTURAL_CONTEXT_REGISTRY_RESOURCE = "domain_context_registry.json"
ARCHITECTURAL_LEGACY_SHIM_REGISTRY_KEY = "domain_legacy_path_shims"
ARCHITECTURAL_LEGACY_SHIM_MODULE_KEY = "legacy_module"
ARCHITECTURAL_CURRENT_SHIM_MODULE_KEY = "current_module"


def load_architectural_fitness_baseline(root):
    """Read the reviewed, versioned architectural scope and measurements."""
    return json.loads((Path(root) / ARCHITECTURAL_PACKAGE_RELATIVE_ROOT / ARCHITECTURAL_BASELINE_RESOURCE).read_text())


def load_architectural_context_registry(root):
    """Read the reviewed module-to-context and access-surface registry."""
    path = Path(root) / ARCHITECTURAL_PACKAGE_RELATIVE_ROOT / ARCHITECTURAL_CONTEXT_REGISTRY_RESOURCE
    registry = json.loads(path.read_text())
    registry[ARCHITECTURAL_BOUNDARY_SHIM_KEY] = sorted(
        declaration[ARCHITECTURAL_LEGACY_SHIM_MODULE_KEY]
        for declaration in registry.get(ARCHITECTURAL_LEGACY_SHIM_REGISTRY_KEY, ())
    )
    return registry


def measure_architectural_boundary_violations(edges, registry):
    """Return cross-context non-surface edges and edges lacking complete context coverage."""
    shim_map = {
        declaration[ARCHITECTURAL_LEGACY_SHIM_MODULE_KEY]:
            declaration[ARCHITECTURAL_CURRENT_SHIM_MODULE_KEY]
        for declaration in registry.get(ARCHITECTURAL_LEGACY_SHIM_REGISTRY_KEY, ())
    }
    edges = collapse_architectural_shim_edges(edges, shim_map)
    package = registry["contexts"][0]["surface_module"].split(".", 1)[0]
    home_contexts = {
        module: declaration["home_context"]
        for module, declaration in registry.get("split_modules", {}).items()
    }
    module_context = {}
    context_surfaces = {}
    for context in registry["contexts"]:
        context_id = context["id"]
        surface = context["surface_module"]
        context_surfaces[context_id] = surface
        module_context[surface] = context_id
        for module in context["backing_modules"]:
            if home_contexts.get(module, context_id) == context_id:
                qualified = package if module == "__init__" else package + "." + module
                module_context[qualified] = context_id
    exempt_sources = set(registry.get(ARCHITECTURAL_BOUNDARY_SHIM_KEY, ()))
    violations = set()
    unassigned = set()
    for source, targets in edges.items():
        for target in targets:
            source_context = module_context.get(source)
            target_context = module_context.get(target)
            if source_context is None or target_context is None:
                unassigned.add((source, target))
            elif (
                source not in exempt_sources
                and source_context != target_context
                and target != context_surfaces[target_context]
            ):
                violations.add((source, target))
    return sorted(violations), sorted(unassigned)


def measure_architectural_toplevel_flat_modules(root, settings):
    """Return direct-child package modules, excluding the package initializer."""
    package_root = Path(root) / settings["source_root"]
    return sorted(
        path.name for path in package_root.glob("*.py")
        if path.name != "__init__.py"
    )


def regenerate_architectural_boundary_baseline(root):
    """Explicitly record the current boundary set when it tightens the reviewed ratchet."""
    root = Path(root).resolve()
    settings = load_architectural_fitness_baseline(root)
    registry = load_architectural_context_registry(root)
    units = collect_architectural_source_units(root, settings)
    measured, unassigned = measure_architectural_boundary_violations(
        collect_architectural_import_edges(units, legacy_modules={
            declaration[ARCHITECTURAL_LEGACY_SHIM_MODULE_KEY]
            for declaration in registry.get(ARCHITECTURAL_LEGACY_SHIM_REGISTRY_KEY, ())
        }), registry,
    )
    recorded = {tuple(edge) for edge in settings.get(ARCHITECTURAL_BOUNDARY_BASELINE_KEY, ())}
    if recorded and not set(measured) < recorded:
        return {"updated": False, "violations": measured, "unassigned": unassigned}
    baseline_path = root / ARCHITECTURAL_PACKAGE_RELATIVE_ROOT / ARCHITECTURAL_BASELINE_RESOURCE

    def tighten_architectural_boundary_baseline(data):
        data[ARCHITECTURAL_BOUNDARY_BASELINE_KEY] = [list(edge) for edge in measured]

    config.update_json(
        baseline_path,
        tighten_architectural_boundary_baseline,
        reason="architectural context boundary ratchet regeneration",
        keep_backups=0,
        required=True,
    )
    return {"updated": True, "violations": measured, "unassigned": unassigned}


def regenerate_architectural_legacy_flat_module_baseline(root):
    """Explicitly record top-level flat modules only when legacy debt shrinks."""
    root = Path(root).resolve()
    settings = load_architectural_fitness_baseline(root)
    measured = measure_architectural_toplevel_flat_modules(root, settings)
    recorded = set(settings.get(ARCHITECTURAL_LEGACY_FLAT_MODULE_BASELINE_KEY, ()))
    if recorded and not set(measured) < recorded:
        return {"updated": False, "modules": measured}
    baseline_path = root / ARCHITECTURAL_PACKAGE_RELATIVE_ROOT / ARCHITECTURAL_BASELINE_RESOURCE

    def tighten_architectural_legacy_flat_module_baseline(data):
        data[ARCHITECTURAL_LEGACY_FLAT_MODULE_BASELINE_KEY] = measured

    config.update_json(
        baseline_path,
        tighten_architectural_legacy_flat_module_baseline,
        reason="architectural top-level flat-module ratchet regeneration",
        keep_backups=0,
        required=True,
    )
    return {"updated": True, "modules": measured}


def collect_architectural_source_units(root, settings):
    """Parse every package source file, including nested packages, without importing it."""
    package_root = Path(root) / settings["source_root"]
    units = {}
    for path in sorted(package_root.rglob("*.py")):
        relative = path.relative_to(package_root).with_suffix("")
        parts = relative.parts
        if parts[-1] == "__init__":
            parts = parts[:-1]
        name = ".".join((settings["package"], *parts))
        units[name] = (path, ast.parse(path.read_text(), filename=str(path)))
    if not units:
        raise ValueError("Architectural source scope is empty")
    return units


def resolve_architectural_dynamic_import_target(node, *, package, root_package):
    """Resolve a literal package dynamic import, leaving semantic expressions unresolved."""
    if not isinstance(node, ast.Call):
        return None
    callee = node.func
    recognized = (
        isinstance(callee, ast.Name) and callee.id in {"import_module", "__import__"}
        or isinstance(callee, ast.Attribute) and isinstance(callee.value, ast.Name)
        and callee.value.id == "importlib" and callee.attr == "import_module"
    )
    if not recognized:
        return None
    name_node = node.args[0] if node.args else next(
        (keyword.value for keyword in node.keywords if keyword.arg == "name"), None,
    )
    if not isinstance(name_node, ast.Constant) or not isinstance(name_node.value, str):
        return None
    target = name_node.value
    package_node = node.args[1] if len(node.args) > 1 else next(
        (keyword.value for keyword in node.keywords if keyword.arg == "package"), None,
    )
    package_name = package if isinstance(package_node, ast.Name) and package_node.id == "__package__" else None
    if isinstance(package_node, ast.Constant) and isinstance(package_node.value, str):
        package_name = package_node.value
    if target.startswith(".") and package_name:
        target = importlib.util.resolve_name(target, package_name)
    elif package_name and target != root_package and not target.startswith(root_package + "."):
        target = package_name + "." + target
    return target if target == root_package or target.startswith(root_package + ".") else None


def architectural_dynamic_import_is_unresolved(node, *, package, root_package):
    """Identify package-anchored dynamic imports whose target AST is not literal."""
    if not isinstance(node, ast.Call):
        return False
    callee = node.func
    recognized = (
        isinstance(callee, ast.Name) and callee.id in {"import_module", "__import__"}
        or isinstance(callee, ast.Attribute) and isinstance(callee.value, ast.Name)
        and callee.value.id == "importlib" and callee.attr == "import_module"
    )
    if not recognized:
        return False
    name_node = node.args[0] if node.args else next(
        (keyword.value for keyword in node.keywords if keyword.arg == "name"), None,
    )
    if name_node is None or isinstance(name_node, ast.Constant) and isinstance(name_node.value, str):
        return False
    package_node = node.args[1] if len(node.args) > 1 else next(
        (keyword.value for keyword in node.keywords if keyword.arg == "package"), None,
    )
    anchored_package = (
        isinstance(package_node, ast.Name) and package_node.id == "__package__"
        or isinstance(package_node, ast.Constant) and package_node.value in {package, root_package}
    )
    literal_fragments = (
        part.value for part in ast.walk(name_node)
        if isinstance(part, ast.Constant) and isinstance(part.value, str)
    )
    return anchored_package or any(
        fragment == root_package or fragment.startswith(root_package + ".") for fragment in literal_fragments
    )


def collect_architectural_import_edges(units, legacy_modules=()):
    """Resolve absolute and relative Python imports, including deferred imports."""
    edges = {name: set() for name in units}
    legacy_modules = set(legacy_modules)
    root_package = min(units, key=lambda candidate: (candidate.count("."), len(candidate))).split(".")[0]
    for name, (path, tree) in units.items():
        package = name if path.stem == "__init__" else name.rpartition(".")[0]
        for node in ast.walk(tree):
            candidates = []
            if isinstance(node, ast.Import):
                candidates.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                base = importlib.util.resolve_name("." * node.level + (node.module or ""), package) if node.level else node.module
                if base:
                    for alias in node.names:
                        imported_module = base + "." + alias.name
                        candidates.append(
                            imported_module
                            if imported_module in units or imported_module in legacy_modules
                            else base
                        )
            elif isinstance(node, ast.Call):
                target = resolve_architectural_dynamic_import_target(
                    node, package=package, root_package=root_package,
                )
                if target:
                    candidates.append(target)
            for candidate in candidates:
                if candidate in legacy_modules:
                    edges.setdefault(candidate, set())
                if candidate in edges and candidate != name:
                    edges[name].add(candidate)
                elif candidate == name and isinstance(node, ast.Import):
                    edges[name].add(candidate)
    return edges


def collect_unresolved_architectural_dynamic_imports(units, root):
    """Return stable call sites for package dynamic imports that cannot become graph edges."""
    root = Path(root).resolve()
    root_package = min(units, key=lambda candidate: (candidate.count("."), len(candidate))).split(".")[0]
    findings = set()
    for name, (path, tree) in units.items():
        package = name if path.stem == "__init__" else name.rpartition(".")[0]
        for node in ast.walk(tree):
            if architectural_dynamic_import_is_unresolved(node, package=package, root_package=root_package):
                findings.add(f"{path.resolve().relative_to(root).as_posix()}:{node.lineno}")
    return findings


def find_architectural_import_cycles(edges):
    """Return deterministic strongly connected components, without runtime imports."""
    index = {}
    low = {}
    stack = []
    active = set()
    components = []

    def visit_architectural_import_vertex(vertex):
        index[vertex] = low[vertex] = len(index)
        stack.append(vertex)
        active.add(vertex)
        for target in sorted(edges[vertex]):
            if target not in index:
                visit_architectural_import_vertex(target)
                low[vertex] = min(low[vertex], low[target])
            elif target in active:
                low[vertex] = min(low[vertex], index[target])
        if low[vertex] == index[vertex]:
            component = []
            while True:
                target = stack.pop()
                active.remove(target)
                component.append(target)
                if target == vertex:
                    break
            if len(component) > 1 or vertex in edges[vertex]:
                components.append(sorted(component))

    for vertex in sorted(edges):
        if vertex not in index:
            visit_architectural_import_vertex(vertex)
    return sorted(components)


def measure_architectural_cycle_magnitude(edges):
    """Measure the largest cyclic component and all intra-component edges."""
    components = find_architectural_import_cycles(edges)
    cyclic_edges = {
        (owner, target)
        for component in components
        for owner in component
        for target in edges[owner]
        if target in component
    }
    return {
        "components": components,
        "cyclic_edges": cyclic_edges,
        "largest_scc_size": max(map(len, components), default=0),
        "cyclic_edge_count": len(cyclic_edges),
    }


def collapse_architectural_shim_edges(edges, shim_map):
    """Merge legacy and current module names into one logical import graph."""
    collapsed_edges = {}
    for source, targets in edges.items():
        logical_source = shim_map.get(source, source)
        collapsed_edges.setdefault(logical_source, set())
        for target in targets:
            logical_target = shim_map.get(target, target)
            collapsed_edges.setdefault(logical_target, set())
            collapsed_edges[logical_source].add(logical_target)
    return collapsed_edges


def measure_architectural_cyclic_edge_ratchet(edges, recorded_edges, shim_map):
    """Compare cyclic edges in the canonical current-module namespace."""
    logical_magnitude = measure_architectural_cycle_magnitude(
        collapse_architectural_shim_edges(edges, shim_map)
    )
    normalized_recorded_edges = {
        (shim_map.get(source, source), shim_map.get(target, target))
        for source, target in recorded_edges
    }
    return {
        "magnitude": logical_magnitude,
        "recorded_edges": normalized_recorded_edges,
        "violations": logical_magnitude["cyclic_edges"] - normalized_recorded_edges,
    }


def resolve_architectural_layer_contract(edges, contract, package, shim_map):
    """Resolve declared foundations to current modules and validate layer scope."""
    foundations = {
        shim_map.get(module, module)
        for module in contract["independent_foundations"]
    }
    applications = {
        module for module in set(edges) - foundations - {package}
        if not any(foundation.startswith(module + ".") for foundation in foundations)
    }
    if not foundations <= set(edges) or not applications:
        raise ValueError("Layer contract scope is missing a declared foundation or application layer")
    return foundations, applications


def regenerate_architectural_cycle_magnitude_baseline(root):
    """Explicitly tighten cycle-magnitude ceilings to the current measurement."""
    root = Path(root).resolve()
    settings = load_architectural_fitness_baseline(root)
    units = collect_architectural_source_units(root, settings)
    registry = load_architectural_context_registry(root)
    shim_map = {
        declaration[ARCHITECTURAL_LEGACY_SHIM_MODULE_KEY]: declaration[ARCHITECTURAL_CURRENT_SHIM_MODULE_KEY]
        for declaration in registry.get(ARCHITECTURAL_LEGACY_SHIM_REGISTRY_KEY, ())
    }
    edges = collect_architectural_import_edges(units, legacy_modules=shim_map)
    magnitude = measure_architectural_cycle_magnitude(collapse_architectural_shim_edges(edges, shim_map))
    dynamic_imports = sorted(collect_unresolved_architectural_dynamic_imports(units, root))
    measured_ceilings = {
        ARCHITECTURAL_LARGEST_SCC_CEILING_KEY: magnitude["largest_scc_size"],
        ARCHITECTURAL_CYCLIC_EDGE_CEILING_KEY: magnitude["cyclic_edge_count"],
    }
    existing_ceilings = {key: settings.get(key) for key in measured_ceilings}
    recorded_dynamic_imports = settings.get(ARCHITECTURAL_DYNAMIC_IMPORT_BASELINE_KEY)
    dynamic_imports_tighten = recorded_dynamic_imports is None or set(dynamic_imports) < set(recorded_dynamic_imports)
    ceilings_tighten = any(
        existing_ceilings[key] is None or measured_ceilings[key] < existing_ceilings[key]
        for key in measured_ceilings
    )
    if not ceilings_tighten and not dynamic_imports_tighten:
        return {"updated": False, **measured_ceilings, "dynamic_imports": dynamic_imports}
    baseline_path = root / ARCHITECTURAL_PACKAGE_RELATIVE_ROOT / ARCHITECTURAL_BASELINE_RESOURCE

    def tighten_architectural_cycle_magnitude_baseline(data):
        for key, measurement in measured_ceilings.items():
            recorded = data.get(key)
            if recorded is None or measurement < recorded:
                data[key] = measurement
        recorded = data.get(ARCHITECTURAL_DYNAMIC_IMPORT_BASELINE_KEY)
        if recorded is None or set(dynamic_imports) < set(recorded):
            data[ARCHITECTURAL_DYNAMIC_IMPORT_BASELINE_KEY] = dynamic_imports

    config.update_json(
        baseline_path,
        tighten_architectural_cycle_magnitude_baseline,
        reason="architectural cycle magnitude ratchet regeneration",
        keep_backups=0,
        required=True,
    )
    return {"updated": True, **measured_ceilings, "dynamic_imports": dynamic_imports}


def regenerate_architectural_legacy_shim_count_baseline(
    root, reviewed_justification=None, reviewed_justification_key=None,
):
    """Record shim debt, requiring explicit review only when its ceiling rises."""
    root = Path(root).resolve()
    settings = load_architectural_fitness_baseline(root)
    registry = load_architectural_context_registry(root)
    measured_count = len(registry.get(ARCHITECTURAL_LEGACY_SHIM_REGISTRY_KEY, ()))
    recorded_count = settings.get(ARCHITECTURAL_LEGACY_SHIM_COUNT_CEILING_KEY)
    if recorded_count == measured_count:
        return {"updated": False, "legacy_shim_count": measured_count}
    raising = recorded_count is not None and measured_count > recorded_count
    if raising:
        if not isinstance(reviewed_justification, str) or not reviewed_justification.strip():
            raise ValueError("A non-empty reviewed justification is required to raise legacy shim debt")
        if (
            not isinstance(reviewed_justification_key, str)
            or not reviewed_justification_key.startswith(ARCHITECTURAL_COMPLEXITY_REVIEW_KEY_PREFIX)
            or reviewed_justification_key == ARCHITECTURAL_COMPLEXITY_REVIEW_KEY_PREFIX
        ):
            raise ValueError("The legacy shim justification key must start with 'reviewed_'")
        recorded_justification = settings.get(reviewed_justification_key)
        if recorded_justification is not None and recorded_justification != reviewed_justification:
            raise ValueError(
                f"The justification key already records a different review: {reviewed_justification_key}"
            )
    baseline_path = root / ARCHITECTURAL_PACKAGE_RELATIVE_ROOT / ARCHITECTURAL_BASELINE_RESOURCE

    def record_architectural_legacy_shim_count(data):
        data[ARCHITECTURAL_LEGACY_SHIM_COUNT_CEILING_KEY] = measured_count
        if raising:
            data[reviewed_justification_key] = reviewed_justification

    config.update_json(
        baseline_path,
        record_architectural_legacy_shim_count,
        reason="architectural legacy shim debt ratchet regeneration",
        keep_backups=0,
        required=True,
    )
    return {"updated": True, "legacy_shim_count": measured_count}


def measure_architectural_complexity(root, settings):
    """Measure radon CC maxima and MI per file, rather than inventing grade bars."""
    from radon.complexity import add_inner_blocks, cc_visit
    from radon.metrics import mi_visit

    measurements = {}
    for path, _ in collect_architectural_source_units(root, settings).values():
        source = path.read_text()
        blocks = add_inner_blocks(cc_visit(source))
        measurements[path.relative_to(root).as_posix()] = {
            "max_cc": max((block.complexity for block in blocks), default=0),
            "mi": mi_visit(source, multi=True),
        }
    return measurements


def select_architectural_interpreter_baseline(settings):
    """Require measurements from this exact Python minor tokenizer version."""
    interpreter = ".".join(map(str, sys.version_info[:2]))
    recorded = settings["complexity"]["interpreters"].get(interpreter)
    if recorded is None:
        raise ValueError(f"No reviewed architectural complexity baseline for Python {interpreter}")
    return recorded


def architectural_complexity_metrics_regress(current_metrics, stored_metrics):
    """Return whether measured complexity is absent or worse than its reviewed ratchet."""
    return (
        stored_metrics is None
        or current_metrics["max_cc"] > stored_metrics["max_cc"]
        or current_metrics["mi"] < stored_metrics["mi"]
    )


def regenerate_architectural_complexity_baseline(root, justification=None, justification_key=None):
    """Record only this interpreter's measured regressions after explicit review."""
    root = Path(root).resolve()
    settings = load_architectural_fitness_baseline(root)
    measured_files = measure_architectural_complexity(root, settings)
    recorded = select_architectural_interpreter_baseline(settings)
    updated_files = {
        name: metrics
        for name, metrics in measured_files.items()
        if architectural_complexity_metrics_regress(metrics, recorded["files"].get(name))
    }
    if not updated_files:
        return {"updated_files": (), "justification_key": None, "measured_extremes": recorded["measured_extremes"]}
    if not isinstance(justification, str) or not justification.strip():
        raise ValueError("A non-empty justification is required to record complexity regressions")
    if (
        not isinstance(justification_key, str)
        or not justification_key.startswith(ARCHITECTURAL_COMPLEXITY_REVIEW_KEY_PREFIX)
        or justification_key == ARCHITECTURAL_COMPLEXITY_REVIEW_KEY_PREFIX
    ):
        raise ValueError("The justification key must start with 'reviewed_'")
    recorded_justification = settings["complexity"].get(justification_key)
    if recorded_justification is not None and recorded_justification != justification:
        raise ValueError(f"The justification key already records a different review: {justification_key}")
    baseline_path = root / ARCHITECTURAL_PACKAGE_RELATIVE_ROOT / ARCHITECTURAL_BASELINE_RESOURCE
    regenerated_extremes = {}

    def mutate_architectural_complexity_baseline(data):
        fresh_recorded = select_architectural_interpreter_baseline(data)
        recorded_file_names = list(fresh_recorded["files"])
        fresh_recorded["files"].update(updated_files)
        if recorded_file_names == sorted(recorded_file_names):
            fresh_recorded["files"] = dict(sorted(fresh_recorded["files"].items()))
        regenerated_extremes.update({
            "max_cc": max(metrics["max_cc"] for metrics in fresh_recorded["files"].values()),
            "mi": min(metrics["mi"] for metrics in fresh_recorded["files"].values()),
        })
        fresh_recorded["measured_extremes"] = dict(regenerated_extremes)
        data["complexity"][justification_key] = justification

    config.update_json(
        baseline_path,
        mutate_architectural_complexity_baseline,
        reason="architectural complexity ratchet regeneration",
        # The documented 0 sentinel keeps atomic replace and the always-on path~ backup, while suppressing only
        # redundant timestamped .json history (git is the deep backup) that violates the package-data invariant.
        keep_backups=0,
        required=True,
    )
    return {
        "updated_files": tuple(sorted(updated_files)),
        "justification_key": justification_key,
        "measured_extremes": regenerated_extremes,
    }


def measure_architectural_duplication(root, settings):
    """Count pylint symilar duplicate lines using its complete package comparison."""
    from pylint.checkers.symilar import Symilar

    detector = Symilar(**settings["duplication_options"])
    for path, _ in collect_architectural_source_units(root, settings).values():
        detector.append_stream(path.relative_to(root).as_posix(), io.StringIO(path.read_text()))
    groups = detector._compute_sims()
    return {
        "duplicate_lines": sum(lines * (len(locations) - 1) for lines, locations in groups),
        "groups": len(groups),
    }


def collect_architectural_dead_findings(root, settings):
    """Run vulture at its documented certainty bar, preserving exact finding identity."""
    from vulture import Vulture

    analyzer = Vulture()
    for path, _ in collect_architectural_source_units(root, settings).values():
        analyzer.scan(path.read_text(), filename=str(path))
    return sorted({
        (Path(item.filename).relative_to(root).as_posix(), item.first_lineno, item.name, item.message)
        for item in analyzer.get_unused_code(min_confidence=settings["dead_confidence"])
    })


def run_architectural_fitness_gate(*, context, invariant):
    """Execute a named architectural check with fail-closed dependency handling."""
    root = Path(context["repo_root"]).resolve()
    try:
        settings = load_architectural_fitness_baseline(root)
        violations = []
        detail = ""
        if invariant in {"cycles", "layers", "boundaries"}:
            units = collect_architectural_source_units(root, settings)
            registry = load_architectural_context_registry(root)
            shim_declarations = registry.get(ARCHITECTURAL_LEGACY_SHIM_REGISTRY_KEY, ())
            shim_map = {
                declaration[ARCHITECTURAL_LEGACY_SHIM_MODULE_KEY]:
                    declaration[ARCHITECTURAL_CURRENT_SHIM_MODULE_KEY]
                for declaration in shim_declarations
            }
            edges = collect_architectural_import_edges(units, legacy_modules=shim_map)
            if invariant == "boundaries":
                measured, unassigned = measure_architectural_boundary_violations(edges, registry)
                recorded = {tuple(edge) for edge in settings[ARCHITECTURAL_BOUNDARY_BASELINE_KEY]}
                violations = sorted(set(measured) - recorded)
                detail = (
                    f"No-new cross-context non-surface import ratchet: {len(measured)} measured; "
                    f"{len(recorded)} recorded; {len(violations)} new; "
                    f"unreviewed context coverage edges={len(unassigned)}."
                )
            elif invariant == "cycles":
                physical_magnitude = measure_architectural_cycle_magnitude(edges)
                cyclic_ratchet = measure_architectural_cyclic_edge_ratchet(
                    edges,
                    {tuple(edge) for edge in settings["recorded_cyclic_edges"]},
                    shim_map,
                )
                logical_magnitude = cyclic_ratchet["magnitude"]
                components = logical_magnitude["components"]
                cyclic_edges = logical_magnitude["cyclic_edges"]
                violations = sorted(cyclic_ratchet["violations"])
                dynamic_imports = collect_unresolved_architectural_dynamic_imports(units, root)
                recorded_dynamic_imports = set(settings[ARCHITECTURAL_DYNAMIC_IMPORT_BASELINE_KEY])
                violations.extend(
                    f"unreviewed dynamic package import: {call_site}"
                    for call_site in sorted(dynamic_imports - recorded_dynamic_imports)
                )
                largest_scc_ceiling = settings[ARCHITECTURAL_LARGEST_SCC_CEILING_KEY]
                cyclic_edge_ceiling = settings[ARCHITECTURAL_CYCLIC_EDGE_CEILING_KEY]
                if logical_magnitude["largest_scc_size"] > largest_scc_ceiling:
                    violations.append(
                        f"largest_scc_size: {logical_magnitude['largest_scc_size']} exceeds {largest_scc_ceiling}"
                    )
                if logical_magnitude["cyclic_edge_count"] > cyclic_edge_ceiling:
                    violations.append(
                        f"cyclic_edge_count: {logical_magnitude['cyclic_edge_count']} exceeds {cyclic_edge_ceiling}"
                    )
                shim_count = len(shim_declarations)
                shim_count_ceiling = settings[ARCHITECTURAL_LEGACY_SHIM_COUNT_CEILING_KEY]
                if shim_count > shim_count_ceiling:
                    violations.append(f"legacy_shim_count: {shim_count} exceeds {shim_count_ceiling}")
                tightening = [
                    key for key, current, ceiling in (
                        ("largest_scc_size", logical_magnitude["largest_scc_size"], largest_scc_ceiling),
                        ("cyclic_edge_count", logical_magnitude["cyclic_edge_count"], cyclic_edge_ceiling),
                        ("legacy_shim_count", shim_count, shim_count_ceiling),
                    ) if current < ceiling
                ]
                detail = (
                    f"No-new-cyclic-import-edge ratchet: {len(edges)} modules; {len(components)} existing cyclic "
                    f"components; {len(cyclic_edges)} cyclic edges; {len(violations)} violations. "
                    f"Magnitude logical scc={logical_magnitude['largest_scc_size']} "
                    f"edges={logical_magnitude['cyclic_edge_count']}; "
                    f"physical scc={physical_magnitude['largest_scc_size']} "
                    f"edges={physical_magnitude['cyclic_edge_count']}; shims={shim_count}<={shim_count_ceiling}. "
                    f"Unresolved dynamic package imports={len(dynamic_imports)}; "
                    f"Explicit baseline tightening available for: {', '.join(tightening) or 'none'}. "
                    "Full no-cycles remains reviewed N/A."
                )
            else:
                contract = settings["layer_contract"]
                layer_edges = collapse_architectural_shim_edges(edges, shim_map)
                foundations, applications = resolve_architectural_layer_contract(
                    layer_edges, contract, settings["package"], shim_map,
                )
                for foundation in sorted(foundations):
                    violations.extend(f"Foundation {foundation} imports package module {target}"
                                      for target in sorted(layer_edges[foundation]))
                configuration = "\n".join((
                    "[importlinter]", f"root_package = {settings['package']}",
                    "[importlinter:contract:architectural-foundations]",
                    "name = Application above independently owned foundations",
                    "type = layers", "layers =",
                    "    " + " : ".join(sorted(applications)),
                    "    " + " | ".join(sorted(foundations)),
                ))
                with tempfile.TemporaryDirectory() as directory:
                    configuration_path = Path(directory) / ARCHITECTURAL_LAYER_CONFIGURATION_FILENAME
                    configuration_path.write_text(configuration)
                    environment = os.environ.copy()
                    environment["PYTHONPATH"] = str(root / settings["source_root"]).rsplit(os.sep, 1)[0]
                    completed = subprocess.run(
                        [sys.executable, "-c", "from importlinter.cli import lint_imports_command; lint_imports_command()",
                         "--config", str(configuration_path), "--no-cache", "--no-logo"],
                        cwd=root, env=environment, text=True, capture_output=True, check=False,
                    )
                detail = contract["reason"] + "\n" + completed.stdout + completed.stderr
                if completed.returncode:
                    violations.append(detail)
        elif invariant == "new_code":
            measured = measure_architectural_toplevel_flat_modules(root, settings)
            recorded = set(settings[ARCHITECTURAL_LEGACY_FLAT_MODULE_BASELINE_KEY])
            violations = sorted(set(measured) - recorded)
            detail = (
                f"No-new top-level flat-module ratchet: {len(measured)} measured; "
                f"{len(recorded)} recorded; {len(violations)} new."
            )
        elif invariant == "complexity":
            current = measure_architectural_complexity(root, settings)
            recorded = select_architectural_interpreter_baseline(settings)
            for name, metrics in current.items():
                baseline = recorded["files"].get(name)
                if baseline is None:
                    violations.append(f"{name}: new source file needs an explicit reviewed measured complexity baseline")
                    continue
                if architectural_complexity_metrics_regress(metrics, baseline):
                    violations.append(f"{name}: {metrics} regresses {baseline}")
            detail = f"Radon per-file ratchet: {len(current)} files; measured worst CC {recorded['measured_extremes']['max_cc']}, MI {recorded['measured_extremes']['mi']}; new files require explicit measured review"
        elif invariant == "duplication":
            current = measure_architectural_duplication(root, settings)
            baseline = settings["duplication"]
            violations = [f"{key}: {value} exceeds {baseline[key]}" for key, value in current.items() if value > baseline[key]]
            detail = f"Pylint symilar measured {current}; ratchet {baseline}"
        elif invariant == "dead":
            findings = collect_architectural_dead_findings(root, settings)
            allowed = {(row["path"], row["line"], row["name"], row["message"]) for row in settings["dead_allowlist"] if row["reason"]}
            violations = [str(finding) for finding in findings if finding not in allowed]
            detail = f"Vulture {settings['dead_confidence']}% certainty: {len(findings)} findings; {len(allowed)} individually reviewed compatibility exceptions"
        elif invariant == "types":
            targets = settings["type_scope"]
            completed = subprocess.run(
                [sys.executable, "-m", "mypy", "--follow-imports=skip", "--no-incremental", *targets],
                cwd=root, text=True, capture_output=True, check=False,
            )
            detail = completed.stdout + completed.stderr
            if completed.returncode:
                violations.append(detail)
        else:
            raise ValueError(f"Unknown architectural invariant: {invariant}")
        return CapabilityReadinessEvidence("FAIL" if violations else "PASS", detail, tuple(map(str, violations)) or (detail,))
    except (ImportError, OSError, ValueError, SyntaxError) as error:
        return CapabilityReadinessEvidence("FAIL", f"Architectural {invariant} unavailable: {error}")
