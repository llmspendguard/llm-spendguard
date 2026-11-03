"""Every architectural detector must reject its own complete scratch violation."""

import json
from pathlib import Path
import sys
import tempfile

import pytest

from spendguard.architectural_fitness import (
    collapse_architectural_shim_edges,
    collect_architectural_import_edges,
    collect_architectural_source_units,
    collect_unresolved_architectural_dynamic_imports,
    load_architectural_fitness_baseline,
    load_architectural_context_registry,
    measure_architectural_boundary_violations,
    measure_architectural_cycle_magnitude,
    measure_architectural_complexity,
    measure_architectural_duplication,
    regenerate_architectural_complexity_baseline,
    regenerate_architectural_legacy_shim_count_baseline,
    run_architectural_fitness_gate,
    select_architectural_interpreter_baseline,
)

ARCHITECTURAL_FIXTURE_CORE = "fixture_foundation"
ARCHITECTURAL_FIXTURE_APPLICATION = "fixture_application"
ARCHITECTURAL_FIXTURE_REVIEW_KEY = "reviewed_fixture_complexity_regression"
ARCHITECTURAL_FIXTURE_SECOND_REVIEW_KEY = "reviewed_fixture_complexity_regression_second"
ARCHITECTURAL_FIXTURE_REVIEW_JUSTIFICATION = "Scratch regression reviewed for the architectural guard proof."
ARCHITECTURAL_FIXTURE_SECOND_REVIEW_JUSTIFICATION = "A distinct scratch regression received its own review."
ARCHITECTURAL_FIXTURE_SHIM_REVIEW_KEY = "reviewed_fixture_legacy_shim_debt"
ARCHITECTURAL_FIXTURE_SHIM_REVIEW_JUSTIFICATION = "Scratch legacy shim debt reviewed for the guard proof."
ARCHITECTURAL_FIXTURE_TWO_NODE_CEILING = 2
ARCHITECTURAL_FIXTURE_THREE_NODE_CEILING = 3
ARCHITECTURAL_FIXTURE_ONE_EDGE_CEILING = 1
ARCHITECTURAL_FIXTURE_ZERO_CEILING = 0
ARCHITECTURAL_FIXTURE_ONE_SHIM_CEILING = 1
ARCHITECTURAL_FIXTURE_TWO_SHIM_CEILING = 2
ARCHITECTURAL_FIXTURE_DYNAMIC_OWNER = "fixture_dynamic_owner"
ARCHITECTURAL_FIXTURE_DYNAMIC_TARGET = "fixture_dynamic_target"
ARCHITECTURAL_FIXTURE_DYNAMIC_NAME = "dynamic_target_name"
ARCHITECTURAL_FIXTURE_CONTEXT_ALPHA = "fixture_context_alpha"
ARCHITECTURAL_FIXTURE_CONTEXT_BETA = "fixture_context_beta"
ARCHITECTURAL_FIXTURE_ALPHA_MODULE = "spendguard.fixture_alpha"
ARCHITECTURAL_FIXTURE_ALPHA_PEER = "spendguard.fixture_alpha_peer"
ARCHITECTURAL_FIXTURE_BETA_MODULE = "spendguard.fixture_beta"
ARCHITECTURAL_FIXTURE_BETA_SURFACE = "spendguard.store.fixture_beta"
ARCHITECTURAL_FIXTURE_UNASSIGNED_MODULE = "spendguard.fixture_infrastructure"
ARCHITECTURAL_FIXTURE_LEGACY_MODULE = "spendguard.fixture_moved"
ARCHITECTURAL_FIXTURE_CURRENT_MODULE = "spendguard.store.fixture_moved"
ARCHITECTURAL_FIXTURE_SHIM_PEER = "spendguard.fixture_shim_peer"
ARCHITECTURAL_FIXTURE_CLEAN_CORE = "def fixture_foundation_identity(value: int) -> int:\n    return value\n"
ARCHITECTURAL_FIXTURE_CLEAN_APPLICATION = "def fixture_application_identity(value: str) -> str:\n    return value\n"
ARCHITECTURAL_FIXTURE_NESTED_COMPLEXITY = "\n".join((
    "def fixture_foundation_identity(value: int) -> int:",
    "    def fixture_nested_branch(argument):", "        if argument:",
    "            return argument", "        return 0", "    return fixture_nested_branch(value)",
)) + "\n"
ARCHITECTURAL_FIXTURE_MI_REGRESSION = "\n".join((
    "def fixture_foundation_identity(value: int) -> int:",
    "    value = value + value", "    value = value * value",
    "    value = value - value", "    value = value + value",
    "    value = value * value", "    value = value - value",
    "    return value",
)) + "\n"
ARCHITECTURAL_FIXTURE_DUPLICATION = "\n".join((
    "def fixture_repeated_computation(value):", "    alpha = value + 1",
    "    beta = alpha * value", "    gamma = beta + alpha", "    delta = gamma * beta",
    "    epsilon = delta + gamma", "    return epsilon",
)) + "\n"
ARCHITECTURAL_FIXTURE_VIOLATIONS = {
    "cycles": (
        "from . import fixture_application\n" + ARCHITECTURAL_FIXTURE_CLEAN_CORE,
        "from . import fixture_foundation\n" + ARCHITECTURAL_FIXTURE_CLEAN_APPLICATION,
    ),
    "layers": ("from . import fixture_application\n" + ARCHITECTURAL_FIXTURE_CLEAN_CORE, None),
    "complexity": ("def fixture_foundation_identity(value: int) -> int:\n    if value:\n        return value\n    return 0\n", None),
    "duplication": (ARCHITECTURAL_FIXTURE_DUPLICATION, ARCHITECTURAL_FIXTURE_DUPLICATION),
    "dead": ("def fixture_foundation_identity(value: int) -> int:\n    return value\n    print(value)\n", None),
    "types": ("def fixture_foundation_identity(value: int) -> int:\n    return str(value)\n", None),
}
ARCHITECTURAL_CYCLE_GROWTH_CASES = (
    (
        {
            "fixture_growth_alpha": "from . import fixture_growth_beta\n",
            "fixture_growth_beta": "from . import fixture_growth_gamma\n",
            "fixture_growth_gamma": "from . import fixture_growth_alpha\n",
        },
        ARCHITECTURAL_FIXTURE_TWO_NODE_CEILING,
        ARCHITECTURAL_FIXTURE_THREE_NODE_CEILING,
        "largest_scc_size",
    ),
    (
        {
            "fixture_growth_alpha": "from . import fixture_growth_beta\n",
            "fixture_growth_beta": "from . import fixture_growth_alpha\n",
        },
        ARCHITECTURAL_FIXTURE_TWO_NODE_CEILING,
        ARCHITECTURAL_FIXTURE_ONE_EDGE_CEILING,
        "cyclic_edge_count",
    ),
)


def write_architectural_fixture_registry(package, shim_declarations=()):
    """Write the minimal reviewed registry needed by a scratch cycles gate."""
    registry = {"domain_legacy_path_shims": list(shim_declarations)}
    (package / "domain_context_registry.json").write_text(json.dumps(registry))


def architectural_fixture_shim_declaration():
    """Return the complete synthetic legacy-to-current module declaration."""
    return {
        "legacy_module": ARCHITECTURAL_FIXTURE_LEGACY_MODULE,
        "current_module": ARCHITECTURAL_FIXTURE_CURRENT_MODULE,
        "moved_in_phase": "fixture",
    }


def write_architectural_shim_cycle_fixture(tmp_path, settings, magnitude_ceiling, shim_count_ceiling):
    """Create a physical three-node cycle that collapses to a logical two-node cycle."""
    package = tmp_path / settings["source_root"]
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("")
    current_relative_parts = ARCHITECTURAL_FIXTURE_CURRENT_MODULE.split(".")[1:-1]
    current_package = package.joinpath(*current_relative_parts)
    current_package.mkdir()
    (current_package / "__init__.py").write_text("")
    peer_import = f"from . import {ARCHITECTURAL_FIXTURE_SHIM_PEER.rsplit('.', 1)[-1]}\n"
    (package / f"{ARCHITECTURAL_FIXTURE_LEGACY_MODULE.rsplit('.', 1)[-1]}.py").write_text(peer_import)
    (current_package / f"{ARCHITECTURAL_FIXTURE_CURRENT_MODULE.rsplit('.', 1)[-1]}.py").write_text(
        f"from .. import {ARCHITECTURAL_FIXTURE_SHIM_PEER.rsplit('.', 1)[-1]}\n"
    )
    (package / f"{ARCHITECTURAL_FIXTURE_SHIM_PEER.rsplit('.', 1)[-1]}.py").write_text(
        f"from . import {ARCHITECTURAL_FIXTURE_LEGACY_MODULE.rsplit('.', 1)[-1]}\n"
        f"from .{'.'.join(current_relative_parts)} import "
        f"{ARCHITECTURAL_FIXTURE_CURRENT_MODULE.rsplit('.', 1)[-1]}\n"
    )
    declaration = architectural_fixture_shim_declaration()
    write_architectural_fixture_registry(package, (declaration,))
    physical_edges = collect_architectural_import_edges(collect_architectural_source_units(tmp_path, settings))
    logical_edges = collapse_architectural_shim_edges(
        physical_edges,
        {declaration["legacy_module"]: declaration["current_module"]},
    )
    logical_magnitude = measure_architectural_cycle_magnitude(logical_edges)
    settings["recorded_cyclic_edges"] = [list(edge) for edge in sorted(logical_magnitude["cyclic_edges"])]
    settings["recorded_dynamic_spendguard_imports"] = []
    settings["max_largest_scc_size"] = magnitude_ceiling
    settings["max_cyclic_edge_count"] = magnitude_ceiling
    settings["max_legacy_shim_count"] = shim_count_ceiling
    (package / "architectural_fitness_baseline.json").write_text(json.dumps(settings))
    return physical_edges, logical_edges


def architectural_boundary_fixture_registry(*, exempt_sources=()):
    """Build the complete two-context registry used by boundary guard proofs."""
    return {
        "contexts": [
            {
                "id": ARCHITECTURAL_FIXTURE_CONTEXT_ALPHA,
                "surface_module": "spendguard.store.fixture_alpha",
                "backing_modules": ["fixture_alpha", "fixture_alpha_peer"],
            },
            {
                "id": ARCHITECTURAL_FIXTURE_CONTEXT_BETA,
                "surface_module": ARCHITECTURAL_FIXTURE_BETA_SURFACE,
                "backing_modules": ["fixture_beta"],
            },
        ],
        "boundary_exempt_shim_modules": list(exempt_sources),
    }


def test_new_cross_context_nonsurface_import_is_red_then_recorded_green(tmp_path):
    """A new bypass edge fails with its identity and passes only after review."""
    repository = Path(__file__).resolve().parents[1]
    settings = load_architectural_fitness_baseline(repository)
    package = tmp_path / settings["source_root"]
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("")
    (package / "fixture_alpha.py").write_text("from . import fixture_beta\n")
    (package / "fixture_beta.py").write_text("")
    registry = architectural_boundary_fixture_registry()
    (package / "domain_context_registry.json").write_text(json.dumps(registry))
    settings["recorded_cross_context_nonsurface_imports"] = []
    settings["boundary_exempt_shim_modules"] = []
    baseline_path = package / "architectural_fitness_baseline.json"
    baseline_path.write_text(json.dumps(settings))
    context = {"repo_root": tmp_path}
    red = run_architectural_fitness_gate(context=context, invariant="boundaries")
    expected_edge = (ARCHITECTURAL_FIXTURE_ALPHA_MODULE, ARCHITECTURAL_FIXTURE_BETA_MODULE)
    assert red.status == "FAIL", red
    assert str(expected_edge) in str(red.evidence), red
    settings["recorded_cross_context_nonsurface_imports"] = [list(expected_edge)]
    baseline_path.write_text(json.dumps(settings))
    assert run_architectural_fitness_gate(context=context, invariant="boundaries").status == "PASS"


@pytest.mark.parametrize(
    ("edge", "registry", "expected_unassigned"),
    (
        (
            (ARCHITECTURAL_FIXTURE_ALPHA_MODULE, ARCHITECTURAL_FIXTURE_ALPHA_PEER),
            architectural_boundary_fixture_registry(),
            (),
        ),
        (
            (ARCHITECTURAL_FIXTURE_ALPHA_MODULE, ARCHITECTURAL_FIXTURE_BETA_SURFACE),
            architectural_boundary_fixture_registry(),
            (),
        ),
        (
            (ARCHITECTURAL_FIXTURE_ALPHA_MODULE, ARCHITECTURAL_FIXTURE_UNASSIGNED_MODULE),
            architectural_boundary_fixture_registry(),
            ((ARCHITECTURAL_FIXTURE_ALPHA_MODULE, ARCHITECTURAL_FIXTURE_UNASSIGNED_MODULE),),
        ),
        (
            (ARCHITECTURAL_FIXTURE_ALPHA_MODULE, ARCHITECTURAL_FIXTURE_BETA_MODULE),
            architectural_boundary_fixture_registry(exempt_sources=(ARCHITECTURAL_FIXTURE_ALPHA_MODULE,)),
            (),
        ),
    ),
    ids=("intra-context", "through-surface", "unassigned-coverage", "shim-source"),
)
def test_boundary_exemptions_are_green(edge, registry, expected_unassigned):
    """Each declared legal or uncovered boundary class stays out of violations."""
    violations, unassigned = measure_architectural_boundary_violations({edge[0]: {edge[1]}}, registry)
    assert violations == []
    assert tuple(unassigned) == expected_unassigned


def test_real_package_cycle_magnitude_stays_within_recorded_ceilings():
    """The production package cannot silently grow its cyclic import hairball."""
    repository = Path(__file__).resolve().parents[1]
    settings = load_architectural_fitness_baseline(repository)
    edges = collect_architectural_import_edges(collect_architectural_source_units(repository, settings))
    registry = load_architectural_context_registry(repository)
    shim_map = {
        declaration["legacy_module"]: declaration["current_module"]
        for declaration in registry["domain_legacy_path_shims"]
    }
    magnitude = measure_architectural_cycle_magnitude(collapse_architectural_shim_edges(edges, shim_map))
    assert magnitude["largest_scc_size"] <= settings["max_largest_scc_size"]
    assert magnitude["cyclic_edge_count"] <= settings["max_cyclic_edge_count"]


def test_shim_collapse_merges_split_module_and_restores_presplit_magnitude():
    """Legacy and current spellings become one logical graph vertex."""
    presplit_edges = {
        ARCHITECTURAL_FIXTURE_CURRENT_MODULE: {ARCHITECTURAL_FIXTURE_SHIM_PEER},
        ARCHITECTURAL_FIXTURE_SHIM_PEER: {ARCHITECTURAL_FIXTURE_CURRENT_MODULE},
    }
    split_edges = {
        ARCHITECTURAL_FIXTURE_LEGACY_MODULE: {ARCHITECTURAL_FIXTURE_SHIM_PEER},
        ARCHITECTURAL_FIXTURE_CURRENT_MODULE: {ARCHITECTURAL_FIXTURE_SHIM_PEER},
        ARCHITECTURAL_FIXTURE_SHIM_PEER: {
            ARCHITECTURAL_FIXTURE_LEGACY_MODULE,
            ARCHITECTURAL_FIXTURE_CURRENT_MODULE,
        },
    }
    shim_map = {ARCHITECTURAL_FIXTURE_LEGACY_MODULE: ARCHITECTURAL_FIXTURE_CURRENT_MODULE}
    collapsed_edges = collapse_architectural_shim_edges(split_edges, shim_map)
    assert len(split_edges) > len(collapsed_edges)
    assert measure_architectural_cycle_magnitude(collapsed_edges) == measure_architectural_cycle_magnitude(
        presplit_edges
    )


def test_cycle_magnitude_ratchet_enforces_logical_not_physical_graph(tmp_path):
    """A physical-only breach passes, while a logical magnitude breach fails."""
    repository = Path(__file__).resolve().parents[1]
    settings = load_architectural_fitness_baseline(repository)
    physical_edges, logical_edges = write_architectural_shim_cycle_fixture(
        tmp_path,
        settings,
        ARCHITECTURAL_FIXTURE_TWO_NODE_CEILING,
        ARCHITECTURAL_FIXTURE_ONE_SHIM_CEILING,
    )
    physical_magnitude = measure_architectural_cycle_magnitude(physical_edges)
    logical_magnitude = measure_architectural_cycle_magnitude(logical_edges)
    assert physical_magnitude["largest_scc_size"] > ARCHITECTURAL_FIXTURE_TWO_NODE_CEILING
    assert logical_magnitude["largest_scc_size"] == ARCHITECTURAL_FIXTURE_TWO_NODE_CEILING
    context = {"repo_root": tmp_path}
    logical_green = run_architectural_fitness_gate(context=context, invariant="cycles")
    assert logical_green.status == "PASS", logical_green
    assert "logical scc=2 edges=2; physical scc=3 edges=4; shims=1<=1" in logical_green.detail
    baseline_path = tmp_path / settings["source_root"] / "architectural_fitness_baseline.json"
    settings["max_largest_scc_size"] = ARCHITECTURAL_FIXTURE_ONE_EDGE_CEILING
    baseline_path.write_text(json.dumps(settings))
    logical_red = run_architectural_fitness_gate(context=context, invariant="cycles")
    assert logical_red.status == "FAIL", logical_red
    assert "largest_scc_size" in str(logical_red.evidence), logical_red


def test_legacy_shim_count_ceiling_is_red_then_reviewed_green_and_lowered_red(tmp_path):
    """Shim debt cannot rise or remain above a lowered ceiling without review."""
    repository = Path(__file__).resolve().parents[1]
    settings = load_architectural_fitness_baseline(repository)
    write_architectural_shim_cycle_fixture(
        tmp_path,
        settings,
        ARCHITECTURAL_FIXTURE_TWO_NODE_CEILING,
        ARCHITECTURAL_FIXTURE_ZERO_CEILING,
    )
    context = {"repo_root": tmp_path}
    unreviewed_red = run_architectural_fitness_gate(context=context, invariant="cycles")
    assert unreviewed_red.status == "FAIL", unreviewed_red
    assert "legacy_shim_count: 1 exceeds 0" in str(unreviewed_red.evidence), unreviewed_red
    with pytest.raises(ValueError, match="reviewed justification"):
        regenerate_architectural_legacy_shim_count_baseline(tmp_path)
    reviewed = regenerate_architectural_legacy_shim_count_baseline(
        tmp_path,
        reviewed_justification=ARCHITECTURAL_FIXTURE_SHIM_REVIEW_JUSTIFICATION,
        reviewed_justification_key=ARCHITECTURAL_FIXTURE_SHIM_REVIEW_KEY,
    )
    assert reviewed == {"updated": True, "legacy_shim_count": ARCHITECTURAL_FIXTURE_ONE_SHIM_CEILING}
    assert run_architectural_fitness_gate(context=context, invariant="cycles").status == "PASS"
    baseline_path = tmp_path / settings["source_root"] / "architectural_fitness_baseline.json"
    lowered_settings = json.loads(baseline_path.read_text())
    lowered_settings["max_legacy_shim_count"] = ARCHITECTURAL_FIXTURE_TWO_SHIM_CEILING
    baseline_path.write_text(json.dumps(lowered_settings))
    lowered = regenerate_architectural_legacy_shim_count_baseline(tmp_path)
    assert lowered == {"updated": True, "legacy_shim_count": ARCHITECTURAL_FIXTURE_ONE_SHIM_CEILING}
    lowered_settings = json.loads(baseline_path.read_text())
    lowered_settings["max_legacy_shim_count"] = ARCHITECTURAL_FIXTURE_ZERO_CEILING
    baseline_path.write_text(json.dumps(lowered_settings))
    lowered_red = run_architectural_fitness_gate(context=context, invariant="cycles")
    assert lowered_red.status == "FAIL", lowered_red
    assert "legacy_shim_count: 1 exceeds 0" in str(lowered_red.evidence), lowered_red


def test_literal_dynamic_import_is_counted_in_cycle_magnitude(tmp_path):
    """A literal dynamic edge participates in the same SCC magnitude as static edges."""
    repository = Path(__file__).resolve().parents[1]
    settings = load_architectural_fitness_baseline(repository)
    package = tmp_path / settings["source_root"]
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("")
    owner_name = settings["package"] + "." + ARCHITECTURAL_FIXTURE_DYNAMIC_OWNER
    target_name = settings["package"] + "." + ARCHITECTURAL_FIXTURE_DYNAMIC_TARGET
    (package / f"{ARCHITECTURAL_FIXTURE_DYNAMIC_OWNER}.py").write_text(
        f'importlib.import_module("{target_name}")\nimport importlib\n'
    )
    (package / f"{ARCHITECTURAL_FIXTURE_DYNAMIC_TARGET}.py").write_text(
        f"from . import {ARCHITECTURAL_FIXTURE_DYNAMIC_OWNER}\n"
    )
    edges = collect_architectural_import_edges(collect_architectural_source_units(tmp_path, settings))
    magnitude = measure_architectural_cycle_magnitude(edges)
    assert target_name in edges[owner_name]
    assert magnitude["largest_scc_size"] == ARCHITECTURAL_FIXTURE_TWO_NODE_CEILING
    assert magnitude["cyclic_edge_count"] == ARCHITECTURAL_FIXTURE_TWO_NODE_CEILING


def test_new_nonliteral_dynamic_import_fails_until_recorded(tmp_path):
    """An unresolved package dynamic import is red, then green only when explicitly recorded."""
    repository = Path(__file__).resolve().parents[1]
    settings = load_architectural_fitness_baseline(repository)
    package = tmp_path / settings["source_root"]
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("")
    write_architectural_fixture_registry(package)
    dynamic_source = package / f"{ARCHITECTURAL_FIXTURE_DYNAMIC_OWNER}.py"
    dynamic_source.write_text(
        "import importlib\n"
        f"{ARCHITECTURAL_FIXTURE_DYNAMIC_NAME} = 'unused'\n"
        f'importlib.import_module(f".{{{ARCHITECTURAL_FIXTURE_DYNAMIC_NAME}}}", __package__)\n'
    )
    settings["recorded_cyclic_edges"] = []
    settings["recorded_dynamic_spendguard_imports"] = []
    settings["max_largest_scc_size"] = 0
    settings["max_cyclic_edge_count"] = 0
    baseline_path = package / "architectural_fitness_baseline.json"
    baseline_path.write_text(json.dumps(settings))
    context = {"repo_root": tmp_path}
    red = run_architectural_fitness_gate(context=context, invariant="cycles")
    assert red.status == "FAIL", red
    assert "unreviewed dynamic package import" in str(red.evidence), red
    units = collect_architectural_source_units(tmp_path, settings)
    recorded_call_sites = sorted(collect_unresolved_architectural_dynamic_imports(units, tmp_path))
    assert len(recorded_call_sites) == 1
    settings["recorded_dynamic_spendguard_imports"] = recorded_call_sites
    baseline_path.write_text(json.dumps(settings))
    green = run_architectural_fitness_gate(context=context, invariant="cycles")
    assert green.status == "PASS", green


@pytest.mark.parametrize(
    ("module_imports", "largest_scc_ceiling", "cyclic_edge_ceiling", "expected_violation"),
    ARCHITECTURAL_CYCLE_GROWTH_CASES,
    ids=("larger-scc", "more-cyclic-edges"),
)
def test_cycle_magnitude_gate_fails_when_independent_ceiling_grows(
    tmp_path, module_imports, largest_scc_ceiling, cyclic_edge_ceiling, expected_violation,
):
    """Each magnitude ceiling independently turns the production gate red."""
    repository = Path(__file__).resolve().parents[1]
    settings = load_architectural_fitness_baseline(repository)
    package = tmp_path / settings["source_root"]
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("")
    write_architectural_fixture_registry(package)
    for module_name, source in module_imports.items():
        (package / f"{module_name}.py").write_text(source)
    edges = collect_architectural_import_edges(collect_architectural_source_units(tmp_path, settings))
    magnitude = measure_architectural_cycle_magnitude(edges)
    settings["recorded_cyclic_edges"] = [list(edge) for edge in sorted(magnitude["cyclic_edges"])]
    settings["max_largest_scc_size"] = largest_scc_ceiling
    settings["max_cyclic_edge_count"] = cyclic_edge_ceiling
    (package / "architectural_fitness_baseline.json").write_text(json.dumps(settings))
    red = run_architectural_fitness_gate(context={"repo_root": tmp_path}, invariant="cycles")
    assert red.status == "FAIL", red
    assert expected_violation in str(red.evidence), red


def test_complexity_regeneration_preserves_active_ratchet(tmp_path):
    """A surgical reviewed refresh preserves other ratchets and remains active."""
    repository = Path(__file__).resolve().parents[1]
    settings = load_architectural_fitness_baseline(repository)
    package = tmp_path / settings["source_root"]
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("")
    core = package / (ARCHITECTURAL_FIXTURE_CORE + ".py")
    application = package / (ARCHITECTURAL_FIXTURE_APPLICATION + ".py")
    core.write_text(ARCHITECTURAL_FIXTURE_CLEAN_CORE)
    application.write_text(ARCHITECTURAL_FIXTURE_CLEAN_APPLICATION)
    measured = measure_architectural_complexity(tmp_path, settings)
    core_relative = core.relative_to(tmp_path).as_posix()
    application_relative = application.relative_to(tmp_path).as_posix()
    preserved_application_metrics = dict(measured[application_relative])
    preserved_application_metrics["max_cc"] += 1
    settings["complexity"]["interpreters"] = {
        ".".join(map(str, sys.version_info[:2])): {
            "files": dict(measured, **{application_relative: preserved_application_metrics}),
            "measured_extremes": {"max_cc": 0, "mi": 0.0},
        }
    }
    baseline_path = package / "architectural_fitness_baseline.json"
    baseline_path.write_text(json.dumps(settings))
    core.write_text(ARCHITECTURAL_FIXTURE_NESTED_COMPLEXITY)
    with pytest.raises(ValueError, match="non-empty justification"):
        regenerate_architectural_complexity_baseline(tmp_path)
    assert json.loads(baseline_path.read_text())["complexity"]["interpreters"][".".join(map(str, sys.version_info[:2]))]["files"][application_relative] == preserved_application_metrics
    regeneration = regenerate_architectural_complexity_baseline(
        tmp_path,
        justification=ARCHITECTURAL_FIXTURE_REVIEW_JUSTIFICATION,
        justification_key=ARCHITECTURAL_FIXTURE_REVIEW_KEY,
    )
    assert regeneration["updated_files"] == (core_relative,)
    regenerated_settings = json.loads(baseline_path.read_text())
    assert regenerated_settings["complexity"]["interpreters"][".".join(map(str, sys.version_info[:2]))]["files"][application_relative] == preserved_application_metrics
    context = {"repo_root": tmp_path}
    assert run_architectural_fitness_gate(context=context, invariant="complexity").status == "PASS"
    unchanged_baseline = baseline_path.read_bytes()
    no_op = regenerate_architectural_complexity_baseline(tmp_path)
    assert no_op["updated_files"] == ()
    assert baseline_path.read_bytes() == unchanged_baseline
    idempotently_reviewed_source = package / "fixture_idempotently_reviewed_source.py"
    idempotently_reviewed_source.write_text(ARCHITECTURAL_FIXTURE_CLEAN_CORE)
    idempotent_regeneration = regenerate_architectural_complexity_baseline(
        tmp_path,
        justification=ARCHITECTURAL_FIXTURE_REVIEW_JUSTIFICATION,
        justification_key=ARCHITECTURAL_FIXTURE_REVIEW_KEY,
    )
    idempotently_reviewed_relative = idempotently_reviewed_source.relative_to(tmp_path).as_posix()
    assert idempotent_regeneration["updated_files"] == (idempotently_reviewed_relative,)
    assert json.loads(baseline_path.read_text())["complexity"][ARCHITECTURAL_FIXTURE_REVIEW_KEY] == ARCHITECTURAL_FIXTURE_REVIEW_JUSTIFICATION
    idempotently_reviewed_source.unlink()
    distinctly_reviewed_source = package / "fixture_distinctly_reviewed_source.py"
    distinctly_reviewed_source.write_text(ARCHITECTURAL_FIXTURE_CLEAN_CORE)
    distinct_regeneration = regenerate_architectural_complexity_baseline(
        tmp_path,
        justification=ARCHITECTURAL_FIXTURE_SECOND_REVIEW_JUSTIFICATION,
        justification_key=ARCHITECTURAL_FIXTURE_SECOND_REVIEW_KEY,
    )
    distinctly_reviewed_relative = distinctly_reviewed_source.relative_to(tmp_path).as_posix()
    assert distinct_regeneration["updated_files"] == (distinctly_reviewed_relative,)
    distinctly_reviewed_source.unlink()
    core.write_text(ARCHITECTURAL_FIXTURE_NESTED_COMPLEXITY + "\n" + ARCHITECTURAL_FIXTURE_MI_REGRESSION)
    regressed_metrics = measure_architectural_complexity(tmp_path, regenerated_settings)[core_relative]
    recorded_metrics = regenerated_settings["complexity"]["interpreters"][".".join(map(str, sys.version_info[:2]))]["files"][core_relative]
    assert regressed_metrics["mi"] < recorded_metrics["mi"]
    assert run_architectural_fitness_gate(context=context, invariant="complexity").status == "FAIL"
    with pytest.raises(ValueError, match="already records a different review"):
        regenerate_architectural_complexity_baseline(
            tmp_path,
            justification=ARCHITECTURAL_FIXTURE_SECOND_REVIEW_JUSTIFICATION,
            justification_key=ARCHITECTURAL_FIXTURE_REVIEW_KEY,
        )


def test_architectural_violation_red_then_restored_green(tmp_path, architectural_invariant):
    """Run the production gate on a complete scratch package, then restore it."""
    repository = Path(__file__).resolve().parents[1]
    settings = load_architectural_fitness_baseline(repository)
    package = tmp_path / settings["source_root"]
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("")
    write_architectural_fixture_registry(package)
    core = package / (ARCHITECTURAL_FIXTURE_CORE + ".py")
    application = package / (ARCHITECTURAL_FIXTURE_APPLICATION + ".py")
    core.write_text(ARCHITECTURAL_FIXTURE_CLEAN_CORE)
    application.write_text(ARCHITECTURAL_FIXTURE_CLEAN_APPLICATION)
    settings["layer_contract"]["independent_foundations"] = [settings["package"] + "." + ARCHITECTURAL_FIXTURE_CORE]
    settings["recorded_cyclic_edges"] = []
    settings["dead_allowlist"] = []
    settings["type_scope"] = [core.relative_to(tmp_path).as_posix()]
    select_architectural_interpreter_baseline(settings)["files"] = measure_architectural_complexity(tmp_path, settings)
    settings["duplication"] = measure_architectural_duplication(tmp_path, settings)
    (package / "architectural_fitness_baseline.json").write_text(json.dumps(settings))
    context = {"repo_root": tmp_path}
    initial = run_architectural_fitness_gate(context=context, invariant=architectural_invariant)
    assert initial.status == "PASS", initial
    changed_core, changed_application = ARCHITECTURAL_FIXTURE_VIOLATIONS[architectural_invariant]
    core.write_text(changed_core)
    if changed_application is not None:
        application.write_text(changed_application)
    red = run_architectural_fitness_gate(context=context, invariant=architectural_invariant)
    assert red.status == "FAIL", red
    core.write_text(ARCHITECTURAL_FIXTURE_CLEAN_CORE)
    application.write_text(ARCHITECTURAL_FIXTURE_CLEAN_APPLICATION)
    restored = run_architectural_fitness_gate(context=context, invariant=architectural_invariant)
    assert restored.status == "PASS", restored
    if architectural_invariant == "layers":
        core.write_text("import spendguard\n" + ARCHITECTURAL_FIXTURE_CLEAN_CORE)
        assert run_architectural_fitness_gate(context=context, invariant=architectural_invariant).status == "FAIL"
        core.write_text(ARCHITECTURAL_FIXTURE_CLEAN_CORE)
        assert run_architectural_fitness_gate(context=context, invariant=architectural_invariant).status == "PASS"
    if architectural_invariant == "complexity":
        core.write_text(ARCHITECTURAL_FIXTURE_NESTED_COMPLEXITY)
        nested_metrics = measure_architectural_complexity(tmp_path, settings)[core.relative_to(tmp_path).as_posix()]
        nested_baseline = select_architectural_interpreter_baseline(settings)["files"][core.relative_to(tmp_path).as_posix()]
        assert nested_metrics["max_cc"] > nested_baseline["max_cc"]
        assert run_architectural_fitness_gate(context=context, invariant=architectural_invariant).status == "FAIL"
        core.write_text(ARCHITECTURAL_FIXTURE_CLEAN_CORE)
        assert run_architectural_fitness_gate(context=context, invariant=architectural_invariant).status == "PASS"
        core.write_text(ARCHITECTURAL_FIXTURE_MI_REGRESSION)
        mi_metrics = measure_architectural_complexity(tmp_path, settings)[core.relative_to(tmp_path).as_posix()]
        mi_baseline = select_architectural_interpreter_baseline(settings)["files"][core.relative_to(tmp_path).as_posix()]
        assert mi_metrics["max_cc"] == mi_baseline["max_cc"]
        assert mi_metrics["mi"] < mi_baseline["mi"]
        assert run_architectural_fitness_gate(context=context, invariant=architectural_invariant).status == "FAIL"
        core.write_text(ARCHITECTURAL_FIXTURE_CLEAN_CORE)
        assert run_architectural_fitness_gate(context=context, invariant=architectural_invariant).status == "PASS"
        unreviewed_source = package / "fixture_unreviewed_source.py"
        unreviewed_source.write_text(ARCHITECTURAL_FIXTURE_CLEAN_CORE)
        assert run_architectural_fitness_gate(context=context, invariant=architectural_invariant).status == "FAIL"
        unreviewed_source.unlink()
        assert run_architectural_fitness_gate(context=context, invariant=architectural_invariant).status == "PASS"
        settings["complexity"]["interpreters"] = {}
        (package / "architectural_fitness_baseline.json").write_text(json.dumps(settings))
        unsupported = run_architectural_fitness_gate(context=context, invariant=architectural_invariant)
        assert unsupported.status == "FAIL"
        assert "No reviewed architectural complexity baseline" in unsupported.detail


if __name__ == "__main__":
    with tempfile.TemporaryDirectory() as architectural_boundary_directory:
        test_new_cross_context_nonsurface_import_is_red_then_recorded_green(
            Path(architectural_boundary_directory),
        )
    print("context boundaries: NEW BYPASS RED / RECORDED GREEN")
    for boundary_case in (
        (
            (ARCHITECTURAL_FIXTURE_ALPHA_MODULE, ARCHITECTURAL_FIXTURE_ALPHA_PEER),
            architectural_boundary_fixture_registry(),
            (),
        ),
        (
            (ARCHITECTURAL_FIXTURE_ALPHA_MODULE, ARCHITECTURAL_FIXTURE_BETA_SURFACE),
            architectural_boundary_fixture_registry(),
            (),
        ),
        (
            (ARCHITECTURAL_FIXTURE_ALPHA_MODULE, ARCHITECTURAL_FIXTURE_UNASSIGNED_MODULE),
            architectural_boundary_fixture_registry(),
            ((ARCHITECTURAL_FIXTURE_ALPHA_MODULE, ARCHITECTURAL_FIXTURE_UNASSIGNED_MODULE),),
        ),
        (
            (ARCHITECTURAL_FIXTURE_ALPHA_MODULE, ARCHITECTURAL_FIXTURE_BETA_MODULE),
            architectural_boundary_fixture_registry(exempt_sources=(ARCHITECTURAL_FIXTURE_ALPHA_MODULE,)),
            (),
        ),
    ):
        test_boundary_exemptions_are_green(*boundary_case)
    print("context boundaries: INTRA / SURFACE / UNASSIGNED / SHIM GREEN")
    test_real_package_cycle_magnitude_stays_within_recorded_ceilings()
    print("cycle magnitude: REAL PACKAGE WITHIN MEASURED CEILINGS")
    test_shim_collapse_merges_split_module_and_restores_presplit_magnitude()
    print("shim collapse: PHYSICAL NODES MERGED / PRESPLIT MAGNITUDE RESTORED")
    with tempfile.TemporaryDirectory() as logical_magnitude_directory:
        test_cycle_magnitude_ratchet_enforces_logical_not_physical_graph(Path(logical_magnitude_directory))
    print("cycle magnitude: PHYSICAL BREACH GREEN / LOGICAL BREACH RED")
    with tempfile.TemporaryDirectory() as shim_count_directory:
        test_legacy_shim_count_ceiling_is_red_then_reviewed_green_and_lowered_red(
            Path(shim_count_directory),
        )
    print("shim debt: UNREVIEWED RED / REVIEWED GREEN / LOWERED RED")
    with tempfile.TemporaryDirectory() as literal_dynamic_directory:
        test_literal_dynamic_import_is_counted_in_cycle_magnitude(Path(literal_dynamic_directory))
    print("dynamic literal import: EDGE COUNTED / SCC MAGNITUDE COUNTED")
    with tempfile.TemporaryDirectory() as unresolved_dynamic_directory:
        test_new_nonliteral_dynamic_import_fails_until_recorded(Path(unresolved_dynamic_directory))
    print("dynamic nonliteral import: NEW RED / RECORDED GREEN")
    for architectural_growth_case in ARCHITECTURAL_CYCLE_GROWTH_CASES:
        with tempfile.TemporaryDirectory() as architectural_growth_directory:
            test_cycle_magnitude_gate_fails_when_independent_ceiling_grows(
                Path(architectural_growth_directory), *architectural_growth_case,
            )
    print("cycle magnitude: LARGER SCC RED / MORE CYCLIC EDGES RED")
    with tempfile.TemporaryDirectory() as architectural_regeneration_directory:
        test_complexity_regeneration_preserves_active_ratchet(Path(architectural_regeneration_directory))
    print("complexity regeneration: SURGICAL / FAIL-CLOSED / IDEMPOTENT")
    for architectural_proof_invariant in ARCHITECTURAL_FIXTURE_VIOLATIONS:
        with tempfile.TemporaryDirectory() as architectural_proof_directory:
            test_architectural_violation_red_then_restored_green(
                Path(architectural_proof_directory), architectural_proof_invariant,
            )
        print(f"{architectural_proof_invariant}: GREEN / deliberate RED / restored GREEN")
