"""Every architectural detector must reject its own complete scratch violation."""

import json
from pathlib import Path
import sys
import tempfile

import pytest

from spendguard.architectural_fitness import (
    load_architectural_fitness_baseline,
    measure_architectural_complexity,
    measure_architectural_duplication,
    regenerate_architectural_complexity_baseline,
    run_architectural_fitness_gate,
    select_architectural_interpreter_baseline,
)

ARCHITECTURAL_FIXTURE_CORE = "fixture_foundation"
ARCHITECTURAL_FIXTURE_APPLICATION = "fixture_application"
ARCHITECTURAL_FIXTURE_REVIEW_KEY = "reviewed_fixture_complexity_regression"
ARCHITECTURAL_FIXTURE_SECOND_REVIEW_KEY = "reviewed_fixture_complexity_regression_second"
ARCHITECTURAL_FIXTURE_REVIEW_JUSTIFICATION = "Scratch regression reviewed for the architectural guard proof."
ARCHITECTURAL_FIXTURE_SECOND_REVIEW_JUSTIFICATION = "A distinct scratch regression received its own review."
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
    with tempfile.TemporaryDirectory() as architectural_regeneration_directory:
        test_complexity_regeneration_preserves_active_ratchet(Path(architectural_regeneration_directory))
    print("complexity regeneration: SURGICAL / FAIL-CLOSED / IDEMPOTENT")
    for architectural_proof_invariant in ARCHITECTURAL_FIXTURE_VIOLATIONS:
        with tempfile.TemporaryDirectory() as architectural_proof_directory:
            test_architectural_violation_red_then_restored_green(
                Path(architectural_proof_directory), architectural_proof_invariant,
            )
        print(f"{architectural_proof_invariant}: GREEN / deliberate RED / restored GREEN")
