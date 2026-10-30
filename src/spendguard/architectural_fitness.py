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

from .capability_readiness import CapabilityReadinessEvidence

ARCHITECTURAL_BASELINE_RESOURCE = "architectural_fitness_baseline.json"
ARCHITECTURAL_PACKAGE_RELATIVE_ROOT = Path("src") / "spendguard"
ARCHITECTURAL_LAYER_CONFIGURATION_FILENAME = "architecture.ini"


def load_architectural_fitness_baseline(root):
    """Read the reviewed, versioned architectural scope and measurements."""
    return json.loads((Path(root) / ARCHITECTURAL_PACKAGE_RELATIVE_ROOT / ARCHITECTURAL_BASELINE_RESOURCE).read_text())


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


def collect_architectural_import_edges(units):
    """Resolve absolute and relative Python imports, including deferred imports."""
    edges = {name: set() for name in units}
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
                        candidates.append(imported_module if imported_module in units else base)
            for candidate in candidates:
                if candidate in units and candidate != name:
                    edges[name].add(candidate)
                elif candidate == name and isinstance(node, ast.Import):
                    edges[name].add(candidate)
    return edges


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


def measure_architectural_complexity(root, settings):
    """Measure radon CC maxima and MI per file, rather than inventing grade bars."""
    from radon.complexity import cc_visit
    from radon.metrics import mi_visit

    measurements = {}
    for path, _ in collect_architectural_source_units(root, settings).values():
        source = path.read_text()
        blocks = cc_visit(source)
        measurements[path.relative_to(root).as_posix()] = {
            "max_cc": max((block.complexity for block in blocks), default=0),
            "mi": mi_visit(source, multi=True),
        }
    return measurements


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
        if invariant in {"cycles", "layers"}:
            edges = collect_architectural_import_edges(collect_architectural_source_units(root, settings))
            if invariant == "cycles":
                components = find_architectural_import_cycles(edges)
                cyclic_edges = {
                    (owner, target) for component in components for owner in component
                    for target in edges[owner] if target in component
                }
                recorded_edges = {tuple(edge) for edge in settings["recorded_cyclic_edges"]}
                violations = sorted(cyclic_edges - recorded_edges)
                detail = f"No-new-cyclic-import-edge ratchet: {len(edges)} modules; {len(components)} existing cyclic components; {len(violations)} new cyclic edges. Full no-cycles remains reviewed N/A."
            else:
                contract = settings["layer_contract"]
                foundations = set(contract["independent_foundations"])
                applications = set(edges) - foundations - {settings["package"]}
                if not foundations <= set(edges) or not applications:
                    raise ValueError("Layer contract scope is missing a declared foundation or application layer")
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
        elif invariant == "complexity":
            current = measure_architectural_complexity(root, settings)
            recorded = settings["complexity"]
            for name, metrics in current.items():
                baseline = recorded["files"].get(name, recorded["new_file_limits"])
                if metrics["max_cc"] > baseline["max_cc"] or metrics["mi"] < baseline["mi"]:
                    violations.append(f"{name}: {metrics} regresses {baseline}")
            detail = f"Radon per-file ratchet: {len(current)} files; measured worst CC {recorded['new_file_limits']['max_cc']}, MI {recorded['new_file_limits']['mi']}"
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
