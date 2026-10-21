"""Exact-once shard inventory, slow-case faults, artifact gaps, and mutation proof."""
import copy
import importlib.util
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
from unittest.mock import patch
import xml.etree.ElementTree as ET

import pytest
import yaml
from coverage import CoverageData

import _gate_shard_plan as shard_plan

SHARD_GUARD_COUNT = 4
SHARD_GUARD_CASE_COUNT = 13
SHARD_GUARD_BASE_SECONDS = 0.25
SHARD_GUARD_SLOW_SECONDS = 100_000.0
SHARD_GUARD_ARTIFACT_PREFIX = "coverage-probe-"
SHARD_GUARD_CORRUPT_COVERAGE = b"corrupt coverage fault"
SHARD_GUARD_CONFTEST_MODULE = "gate_shard_conftest_guard"
SHARD_GUARD_XML_SUITE_NAME = "gate-shard-guard"
SHARD_GUARD_ZERO = 0
SHARD_GUARD_ONE = 1
SHARD_GUARD_COVERAGE_FILENAME = ".coverage.fixture"
SHARD_GUARD_FIXTURE_NODEID = "tests/test_runner.py::test_script[guard_{index}.py]"
SHARD_GUARD_WORKFLOW = Path(__file__).resolve().parents[SHARD_GUARD_ONE] / ".github" / "workflows" / "ci.yml"
SHARD_GUARD_WORKFLOW_FIELDS = (
    ("test", "strategy", "matrix", "shard-index"),
    ("test", "env", "SPENDGUARD_GATE_SHARD_INDEX"),
    ("test", "strategy", "matrix", "python-version"),
    ("coverage", "strategy", "matrix", "python-version"),
)
SHARD_GUARD_VERSIONS = ["3.11", "3.12"]
SHARD_GUARD_VERSION_JOBS = ("test", "coverage")
SHARD_GUARD_SHARD_MATRIX = '${{ fromJSON(needs.gate-plan.outputs.shards) }}'
SHARD_GUARD_SHARD_INDEX = '${{ matrix.shard-index }}'
SHARD_GUARD_UPLOAD_ACTION = 'actions/upload-artifact'
SHARD_GUARD_DOWNLOAD_ACTION = 'actions/download-artifact'
SHARD_GUARD_VERIFY_COMMAND = 'python tests/_gate_shard_plan.py verify-reports --timings "$CI_GATE_TIMINGS" --reports "$CI_GATE_ARTIFACT_ROOT" --artifact-prefix "${CI_GATE_ARTIFACT_PREFIX}-${{ matrix.python-version }}-" --inventory-name "$CI_GATE_COVERAGE_INVENTORY"'
SHARD_GUARD_COMBINE_COMMAND = 'coverage combine --keep "$CI_GATE_ARTIFACT_ROOT"/*'
SHARD_GUARD_TRANSFER_FIELDS = (
    ("test", SHARD_GUARD_UPLOAD_ACTION, "include-hidden-files", "true"),
    ("test", SHARD_GUARD_UPLOAD_ACTION, "if-no-files-found", "error"),
    ("coverage", SHARD_GUARD_DOWNLOAD_ACTION, "merge-multiple", "false"),
)
SHARD_GUARD_EXECUTION_OVERRIDES = ("if", "continue-on-error", "defaults", "shell", "working-directory")
SHARD_GUARD_EXECUTION_FAULTS = {"if": "false", "continue-on-error": "true"}
SHARD_GUARD_TEST_JOB_ENV = {"SPENDGUARD_GATE_SHARD_INDEX": SHARD_GUARD_SHARD_INDEX}
SHARD_GUARD_NOOP_COMMAND = "true"
SHARD_GUARD_REQUIRED_RUN_BLOCKS = {
    "gate-plan": ((
        'echo "shards=$(python tests/_gate_shard_plan.py matrix --timings "$CI_GATE_TIMINGS")" >> "$GITHUB_OUTPUT"',
    ),),
    "test": (
        ('SPENDGUARD_GATE_TIMINGS="$CI_GATE_TIMINGS" SPENDGUARD_COVERAGE=1 pytest -n auto --junitxml="${CI_GATE_REPORT_PREFIX}-${{ matrix.shard-index }}.xml"',),
        ('python tests/_gate_shard_plan.py snapshot-coverage --directory "$GITHUB_WORKSPACE" --inventory-name "$CI_GATE_COVERAGE_INVENTORY"',),
        ('SPENDGUARD_AUDIT_DIR=src/spendguard spendguard audit --ci',),
    ),
    "coverage": ((
        SHARD_GUARD_VERIFY_COMMAND,
        SHARD_GUARD_COMBINE_COMMAND,
        'coverage report --fail-under=40',
        'coverage report --fail-under=78 --include="*/gate.py,*/guard.py,*/budget.py,*/reconcile.py,*/ledger_sync.py,*/pricing.py,*/attribution.py,*/adapters.py,*/tag.py,*/backfill.py,*/deid.py,*/litellm_adapter.py,*/bedrock_adapter.py"',
    ),),
}
SHARD_GUARD_CONFIG_PATHS = {
    ("env", "CI_GATE_TIMINGS"): ".test_durations.json",
    ("env", "CI_GATE_REPORT_PREFIX"): "gate-report",
    ("env", "CI_GATE_ARTIFACT_PREFIX"): "coverage",
    ("env", "CI_GATE_ARTIFACT_ROOT"): "coverage-artifacts",
    ("env", "CI_GATE_COVERAGE_INVENTORY"): "gate-coverage-manifest.json",
    ("jobs", "gate-plan", "outputs"): {"shards": "${{ steps.plan.outputs.shards }}"},
    ("jobs", "test", "needs"): "gate-plan",
    ("jobs", "coverage", "needs"): "test",
}
SHARD_GUARD_TRANSFER_INPUTS = {
    SHARD_GUARD_UPLOAD_ACTION: {
        "name": "${{ env.CI_GATE_ARTIFACT_PREFIX }}-${{ matrix.python-version }}-${{ matrix.shard-index }}",
        "include-hidden-files": "true", "if-no-files-found": "error",
        "path": '.coverage.*\n${{ env.CI_GATE_COVERAGE_INVENTORY }}\n${{ env.CI_GATE_REPORT_PREFIX }}-${{ matrix.shard-index }}.xml\n',
    },
    SHARD_GUARD_DOWNLOAD_ACTION: {
        "pattern": "${{ env.CI_GATE_ARTIFACT_PREFIX }}-${{ matrix.python-version }}-*",
        "path": "${{ env.CI_GATE_ARTIFACT_ROOT }}", "merge-multiple": "false",
    },
}
SHARD_GUARD_BRANCH_ARCS = {(SHARD_GUARD_ONE, SHARD_GUARD_ONE + SHARD_GUARD_ONE)}
SHARD_GUARD_PRODUCTION_RETURN = "    return partitions\n"
SHARD_GUARD_DROPPED_RETURN = "    partitions[GATE_SHARD_FIRST_INDEX].pop()\n    return partitions\n"
SHARD_GUARD_OVERSIZED_INTEGER = 1 << 4096
SHARD_GUARD_EMPTY_PARTIAL = ".coverage.nonmeasuring"
SHARD_GUARD_BRANCH_CONDITION = '''            if data.measured_files():
                if not data.has_arcs():
                    raise GateShardEvidenceError(f"non-empty coverage partial lacks branch arcs: {path}")
                shard_has_measured_branches = True'''
SHARD_GUARD_OLD_BRANCH_CONDITION = '''            if not data.has_arcs() or not data.measured_files():
                raise GateShardEvidenceError(f"missing branch coverage evidence: {path}")
            shard_has_measured_branches = True'''


def assert_gate_shard_evidence_refused(operation, error_type=shard_plan.GateShardEvidenceError):
    try:
        operation()
    except error_type:
        return
    raise AssertionError("incomplete gate evidence was accepted")


def emit_gate_shard_guard_junit(path, nodeids):
    suite = ET.Element("testsuite", {"name": SHARD_GUARD_XML_SUITE_NAME, "tests": str(len(nodeids)),
        "errors": str(SHARD_GUARD_ZERO), "failures": str(SHARD_GUARD_ZERO), "skipped": str(SHARD_GUARD_ZERO)})
    for nodeid in nodeids:
        module, name = nodeid.split("::")
        ET.SubElement(suite, "testcase", {"classname": str(Path(module).with_suffix("")).replace("/", "."),
            "name": name, "time": str(SHARD_GUARD_BASE_SECONDS)})
    ET.ElementTree(suite).write(path)


def verify_gate_shard_workflow_contract(workflow):
    configuration = yaml.load(workflow, Loader=yaml.BaseLoader)
    assert not set(SHARD_GUARD_EXECUTION_OVERRIDES).intersection(configuration)
    for path, expected in SHARD_GUARD_CONFIG_PATHS.items():
        value = configuration
        for component in path:
            assert component in value
            value = value[component]
        assert value == expected
    jobs = configuration["jobs"]
    for job_name, job in jobs.items():
        assert not set(SHARD_GUARD_EXECUTION_OVERRIDES).intersection(job)
        assert job.get("env", {}) == (SHARD_GUARD_TEST_JOB_ENV if job_name == "test" else {})
        for step in job["steps"]:
            assert not set(SHARD_GUARD_EXECUTION_OVERRIDES).intersection(step)
            assert not step.get("env"), "step environment overrides the gate contract"
    for job_name, required_blocks in SHARD_GUARD_REQUIRED_RUN_BLOCKS.items():
        run_blocks = [tuple(line.strip() for line in step["run"].splitlines()
                            if line.strip() and not line.lstrip().startswith("#"))
                      for step in jobs[job_name]["steps"] if "run" in step]
        for required_block in required_blocks:
            assert run_blocks.count(required_block) == SHARD_GUARD_ONE
    for job_name in SHARD_GUARD_VERSION_JOBS:
        assert jobs[job_name]["strategy"]["matrix"].get("python-version") == SHARD_GUARD_VERSIONS
    assert jobs["test"]["strategy"]["matrix"].get("shard-index") == SHARD_GUARD_SHARD_MATRIX
    assert jobs["test"]["env"].get("SPENDGUARD_GATE_SHARD_INDEX") == SHARD_GUARD_SHARD_INDEX
    for job_name, action, field, expected in SHARD_GUARD_TRANSFER_FIELDS:
        action_steps = [step for step in jobs[job_name]["steps"]
                        if step.get("uses", "").partition("@")[SHARD_GUARD_ZERO] == action]
        assert len(action_steps) == SHARD_GUARD_ONE
        assert action_steps[SHARD_GUARD_ZERO].get("with", {}).get(field) == expected
        assert action_steps[SHARD_GUARD_ZERO].get("with") == SHARD_GUARD_TRANSFER_INPUTS[action]
        steps = jobs[job_name]["steps"]
        run_indices = [index for index, step in enumerate(steps) if "run" in step]
        action_index = steps.index(action_steps[SHARD_GUARD_ZERO])
        if action == SHARD_GUARD_DOWNLOAD_ACTION:
            assert all(action_index < index for index in run_indices
                       if SHARD_GUARD_VERIFY_COMMAND in steps[index]["run"].splitlines())
        else:
            assert all(index < action_index for index in run_indices
                       if tuple(line.strip() for line in steps[index]["run"].splitlines()
                                if line.strip() and not line.lstrip().startswith("#"))
                       in SHARD_GUARD_REQUIRED_RUN_BLOCKS["test"][:-SHARD_GUARD_ONE])
    plan_steps = [step for step in jobs["gate-plan"]["steps"] if step.get("id") == "plan"]
    assert len(plan_steps) == SHARD_GUARD_ONE
    assert tuple(plan_steps[SHARD_GUARD_ZERO].get("run", "").splitlines()) == (
        SHARD_GUARD_REQUIRED_RUN_BLOCKS["gate-plan"][SHARD_GUARD_ZERO])
    # The exact command protocol must appear in executable run fields in order.
    commands = [line.strip() for step in jobs["coverage"]["steps"]
                for line in step.get("run", "").splitlines()]
    assert commands.count(SHARD_GUARD_VERIFY_COMMAND) == SHARD_GUARD_ONE
    assert commands.count(SHARD_GUARD_COMBINE_COMMAND) == SHARD_GUARD_ONE
    assert commands.index(SHARD_GUARD_VERIFY_COMMAND) < commands.index(SHARD_GUARD_COMBINE_COMMAND)


def emit_gate_shard_guard_coverage(directory):
    data = CoverageData(basename=str(directory / SHARD_GUARD_COVERAGE_FILENAME))
    data.add_arcs({str(Path(__file__).resolve()): SHARD_GUARD_BRANCH_ARCS})
    data.write()
    shard_plan.snapshot_gate_shard_coverage(directory)


shard_guard_workflow = SHARD_GUARD_WORKFLOW.read_text()
verify_gate_shard_workflow_contract(shard_guard_workflow)
shard_guard_parsed_workflow = yaml.load(shard_guard_workflow, Loader=yaml.BaseLoader)
for shard_guard_config_path in SHARD_GUARD_CONFIG_PATHS:
    shard_guard_config_fault = copy.deepcopy(shard_guard_parsed_workflow)
    shard_guard_config_parent = shard_guard_config_fault
    for shard_guard_config_component in shard_guard_config_path[:-SHARD_GUARD_ONE]:
        shard_guard_config_parent = shard_guard_config_parent[shard_guard_config_component]
    shard_guard_config_fault["disabled-configuration"] = copy.deepcopy(shard_guard_config_parent)
    shard_guard_config_parent.pop(shard_guard_config_path[-SHARD_GUARD_ONE])
    assert_gate_shard_evidence_refused(
        lambda shard_guard_config_fault=shard_guard_config_fault: verify_gate_shard_workflow_contract(
            yaml.dump(shard_guard_config_fault)), AssertionError)
for shard_guard_transfer_action, shard_guard_transfer_inputs in SHARD_GUARD_TRANSFER_INPUTS.items():
    for shard_guard_transfer_field in shard_guard_transfer_inputs:
        shard_guard_transfer_fault = copy.deepcopy(shard_guard_parsed_workflow)
        for shard_guard_transfer_job in shard_guard_transfer_fault["jobs"].values():
            for shard_guard_transfer_step in shard_guard_transfer_job["steps"]:
                if shard_guard_transfer_step.get("uses", "").partition("@")[SHARD_GUARD_ZERO] == shard_guard_transfer_action:
                    shard_guard_transfer_fault["disabled-configuration"] = copy.deepcopy(shard_guard_transfer_step)
                    shard_guard_transfer_step["with"].pop(shard_guard_transfer_field)
        assert_gate_shard_evidence_refused(
            lambda shard_guard_transfer_fault=shard_guard_transfer_fault: verify_gate_shard_workflow_contract(
                yaml.dump(shard_guard_transfer_fault)), AssertionError)
for shard_guard_job_name, shard_guard_required_blocks in SHARD_GUARD_REQUIRED_RUN_BLOCKS.items():
    for shard_guard_required_block in shard_guard_required_blocks:
        shard_guard_run_fault = copy.deepcopy(shard_guard_parsed_workflow)
        for shard_guard_step in shard_guard_run_fault["jobs"][shard_guard_job_name]["steps"]:
            shard_guard_run_lines = tuple(line.strip() for line in shard_guard_step.get("run", "").splitlines()
                                         if line.strip() and not line.lstrip().startswith("#"))
            if shard_guard_run_lines == shard_guard_required_block:
                shard_guard_run_fault["disabled-configuration"] = copy.deepcopy(shard_guard_step)
                shard_guard_step["run"] = SHARD_GUARD_NOOP_COMMAND
        assert_gate_shard_evidence_refused(
            lambda shard_guard_run_fault=shard_guard_run_fault: verify_gate_shard_workflow_contract(
                yaml.dump(shard_guard_run_fault)), AssertionError)
for shard_guard_job_name, shard_guard_job_configuration in shard_guard_parsed_workflow["jobs"].items():
    for shard_guard_override, shard_guard_override_value in SHARD_GUARD_EXECUTION_FAULTS.items():
        shard_guard_execution_fault = copy.deepcopy(shard_guard_parsed_workflow)
        shard_guard_execution_fault["jobs"][shard_guard_job_name][shard_guard_override] = shard_guard_override_value
        assert_gate_shard_evidence_refused(
            lambda shard_guard_execution_fault=shard_guard_execution_fault: verify_gate_shard_workflow_contract(
                yaml.dump(shard_guard_execution_fault)), AssertionError)
        for shard_guard_step_index in range(len(shard_guard_job_configuration["steps"])):
            shard_guard_execution_fault = copy.deepcopy(shard_guard_parsed_workflow)
            shard_guard_execution_fault["jobs"][shard_guard_job_name]["steps"][shard_guard_step_index][
                shard_guard_override] = shard_guard_override_value
            assert_gate_shard_evidence_refused(
                lambda shard_guard_execution_fault=shard_guard_execution_fault: verify_gate_shard_workflow_contract(
                    yaml.dump(shard_guard_execution_fault)), AssertionError)
    shard_guard_environment_fault = copy.deepcopy(shard_guard_parsed_workflow)
    shard_guard_environment_fault["jobs"][shard_guard_job_name].setdefault("env", {})[
        shard_plan.GATE_SHARD_TIMINGS_ENV] = ""
    assert_gate_shard_evidence_refused(
        lambda shard_guard_environment_fault=shard_guard_environment_fault: verify_gate_shard_workflow_contract(
            yaml.dump(shard_guard_environment_fault)), AssertionError)
    for shard_guard_step_index in range(len(shard_guard_job_configuration["steps"])):
        shard_guard_environment_fault = copy.deepcopy(shard_guard_parsed_workflow)
        shard_guard_environment_fault["jobs"][shard_guard_job_name]["steps"][shard_guard_step_index]["env"] = {
            shard_plan.GATE_SHARD_INDEX_ENV: ""}
        assert_gate_shard_evidence_refused(
            lambda shard_guard_environment_fault=shard_guard_environment_fault: verify_gate_shard_workflow_contract(
                yaml.dump(shard_guard_environment_fault)), AssertionError)
for shard_guard_field_path in SHARD_GUARD_WORKFLOW_FIELDS:
    shard_guard_fault = copy.deepcopy(shard_guard_parsed_workflow)
    shard_guard_field_parent = shard_guard_fault["jobs"]
    for shard_guard_path_component in shard_guard_field_path[:-SHARD_GUARD_ONE]:
        shard_guard_field_parent = shard_guard_field_parent[shard_guard_path_component]
    # Preserve the original configuration under an inactive field: only the schema path counts.
    shard_guard_fault["disabled-configuration"] = copy.deepcopy(shard_guard_field_parent)
    shard_guard_field_parent.pop(shard_guard_field_path[-SHARD_GUARD_ONE])
    assert_gate_shard_evidence_refused(lambda shard_guard_fault=shard_guard_fault: verify_gate_shard_workflow_contract(
        yaml.dump(shard_guard_fault)), AssertionError)
for shard_guard_job, shard_guard_action, shard_guard_field, _ in SHARD_GUARD_TRANSFER_FIELDS:
    shard_guard_fault = copy.deepcopy(shard_guard_parsed_workflow)
    for shard_guard_step in shard_guard_fault["jobs"][shard_guard_job]["steps"]:
        if shard_guard_step.get("uses", "").partition("@")[SHARD_GUARD_ZERO] == shard_guard_action:
            shard_guard_fault["disabled-configuration"] = copy.deepcopy(shard_guard_step)
            shard_guard_step["with"].pop(shard_guard_field)
    assert_gate_shard_evidence_refused(lambda shard_guard_fault=shard_guard_fault: verify_gate_shard_workflow_contract(
        yaml.dump(shard_guard_fault)), AssertionError)
shard_guard_command_fault = copy.deepcopy(shard_guard_parsed_workflow)
for shard_guard_step in shard_guard_command_fault["jobs"]["coverage"]["steps"]:
    if "run" in shard_guard_step:
        shard_guard_step["run"] = shard_guard_step["run"].replace(
            SHARD_GUARD_VERIFY_COMMAND, "", SHARD_GUARD_ONE)
shard_guard_command_fault["disabled-configuration"] = SHARD_GUARD_VERIFY_COMMAND
assert_gate_shard_evidence_refused(lambda: verify_gate_shard_workflow_contract(
    yaml.dump(shard_guard_command_fault)), AssertionError)
shard_guard_order_fault = copy.deepcopy(shard_guard_parsed_workflow)
for shard_guard_step in shard_guard_order_fault["jobs"]["coverage"]["steps"]:
    shard_guard_lines = shard_guard_step.get("run", "").splitlines()
    if SHARD_GUARD_VERIFY_COMMAND in shard_guard_lines and SHARD_GUARD_COMBINE_COMMAND in shard_guard_lines:
        shard_guard_verify_index = shard_guard_lines.index(SHARD_GUARD_VERIFY_COMMAND)
        shard_guard_combine_index = shard_guard_lines.index(SHARD_GUARD_COMBINE_COMMAND)
        shard_guard_lines[shard_guard_verify_index], shard_guard_lines[shard_guard_combine_index] = (
            shard_guard_lines[shard_guard_combine_index], shard_guard_lines[shard_guard_verify_index])
        shard_guard_step["run"] = "\n".join(shard_guard_lines)
assert_gate_shard_evidence_refused(lambda: verify_gate_shard_workflow_contract(
    yaml.dump(shard_guard_order_fault)), AssertionError)


shard_guard_nodeids = [SHARD_GUARD_FIXTURE_NODEID.format(index=index)
                      for index in range(SHARD_GUARD_CASE_COUNT)]
shard_guard_manifest = {"schema_version": shard_plan.GATE_SHARD_SCHEMA_VERSION, "shard_count": SHARD_GUARD_COUNT,
    "durations": {nodeid: SHARD_GUARD_BASE_SECONDS for nodeid in shard_guard_nodeids}}
shard_guard_manifest["durations"][shard_guard_nodeids[SHARD_GUARD_ZERO]] = SHARD_GUARD_SLOW_SECONDS
shard_guard_partitions = shard_plan.distribute_gate_shard_nodeids(shard_guard_nodeids, shard_guard_manifest)
shard_plan.verify_gate_shard_partition(shard_guard_nodeids, shard_guard_partitions)
# Execute the whole actual production module with one deliberate defect in memory.
# Broken source is never installed in the worktree or used by the real suite.
shard_guard_production_source = Path(shard_plan.__file__).read_text()
with tempfile.TemporaryDirectory() as shard_guard_union_temporary:
    shard_guard_union_root = Path(shard_guard_union_temporary)
    shard_guard_empty_data = CoverageData(basename=str(shard_guard_union_root / SHARD_GUARD_EMPTY_PARTIAL))
    shard_guard_empty_data.add_arcs({})
    shard_guard_empty_data.write()
    with pytest.raises(shard_plan.GateShardEvidenceError, match="empty branch coverage union"):
        shard_plan.snapshot_gate_shard_coverage(shard_guard_union_root)
    # Inject a non-measuring process alongside real branch evidence.
    emit_gate_shard_guard_coverage(shard_guard_union_root)
    shard_guard_union_inventory = shard_plan.read_gate_shard_coverage_inventory(shard_guard_union_root)
    assert set(shard_guard_union_inventory) == {SHARD_GUARD_EMPTY_PARTIAL, SHARD_GUARD_COVERAGE_FILENAME}
    assert shard_guard_production_source.count(SHARD_GUARD_BRANCH_CONDITION) == SHARD_GUARD_ONE
    shard_guard_coverage_mutant = {"__name__": SHARD_GUARD_CONFTEST_MODULE}
    exec(compile(shard_guard_production_source.replace(
        SHARD_GUARD_BRANCH_CONDITION, SHARD_GUARD_OLD_BRANCH_CONDITION),
        shard_plan.__file__, "exec"), shard_guard_coverage_mutant)
    # The old per-partial predicate must reject the exact evidence accepted above.
    with pytest.raises(shard_guard_coverage_mutant["GateShardEvidenceError"]):
        shard_guard_coverage_mutant["snapshot_gate_shard_coverage"](shard_guard_union_root)
    shard_plan.snapshot_gate_shard_coverage(shard_guard_union_root)
    # A line-only partial fails even when valid branch evidence is also present.
    shard_guard_empty_data.erase()
    shard_guard_empty_data = CoverageData(basename=str(shard_guard_union_root / SHARD_GUARD_EMPTY_PARTIAL))
    shard_guard_empty_data.add_lines({str(Path(__file__).resolve()): {SHARD_GUARD_ONE}})
    shard_guard_empty_data.write()
    with pytest.raises(shard_plan.GateShardEvidenceError, match="non-empty coverage partial lacks branch arcs"):
        shard_plan.snapshot_gate_shard_coverage(shard_guard_union_root)
    (shard_guard_union_root / SHARD_GUARD_COVERAGE_FILENAME).unlink()
    with pytest.raises(shard_plan.GateShardEvidenceError, match="non-empty coverage partial lacks branch arcs"):
        shard_plan.snapshot_gate_shard_coverage(shard_guard_union_root)
    shard_guard_empty_data.erase()
    shard_guard_empty_data = CoverageData(basename=str(shard_guard_union_root / SHARD_GUARD_EMPTY_PARTIAL))
    shard_guard_empty_data.add_lines({})
    shard_guard_empty_data.write()
    with pytest.raises(shard_plan.GateShardEvidenceError, match="empty branch coverage union"):
        shard_plan.snapshot_gate_shard_coverage(shard_guard_union_root)
print("Coverage union faults refused; empty partial accepted; old predicate mutation rejected; restored PASS")
assert shard_guard_production_source.count(SHARD_GUARD_PRODUCTION_RETURN) == SHARD_GUARD_ONE
shard_guard_mutated_namespace = {"__name__": SHARD_GUARD_CONFTEST_MODULE}
exec(compile(shard_guard_production_source.replace(SHARD_GUARD_PRODUCTION_RETURN,
    SHARD_GUARD_DROPPED_RETURN), shard_plan.__file__, "exec"), shard_guard_mutated_namespace)
shard_guard_mutated_result = shard_guard_mutated_namespace["distribute_gate_shard_nodeids"](
    shard_guard_nodeids, shard_guard_manifest)
assert_gate_shard_evidence_refused(lambda: shard_plan.verify_gate_shard_partition(
    shard_guard_nodeids, shard_guard_mutated_result))
print("PRODUCTION MUTATION FAIL expected: actual algorithm return drops a case; exact-once guard rejects")
assert shard_guard_partitions == shard_plan.distribute_gate_shard_nodeids(
    list(reversed(shard_guard_nodeids)), shard_guard_manifest)
assert shard_guard_partitions[SHARD_GUARD_ZERO] == [shard_guard_nodeids[SHARD_GUARD_ZERO]]
assert_gate_shard_evidence_refused(lambda: shard_plan.distribute_gate_shard_nodeids(
    shard_guard_nodeids + [shard_guard_nodeids[SHARD_GUARD_ZERO]], shard_guard_manifest))
for shard_guard_bad_duration in (-SHARD_GUARD_ONE, float("inf"), float("nan"), True, SHARD_GUARD_OVERSIZED_INTEGER):
    shard_guard_invalid = copy.deepcopy(shard_guard_manifest)
    shard_guard_invalid["durations"][shard_guard_nodeids[SHARD_GUARD_ZERO]] = shard_guard_bad_duration
    assert_gate_shard_evidence_refused(lambda shard_guard_invalid=shard_guard_invalid: shard_plan.distribute_gate_shard_nodeids(
        shard_guard_nodeids, shard_guard_invalid))
shard_guard_missing = copy.deepcopy(shard_guard_manifest)
shard_guard_missing["durations"].pop(shard_guard_nodeids[SHARD_GUARD_ZERO])
assert_gate_shard_evidence_refused(lambda: shard_plan.distribute_gate_shard_nodeids(
    shard_guard_nodeids, shard_guard_missing))
shard_guard_incomplete_collection = list(shard_guard_nodeids)
shard_guard_incomplete_collection.pop()
assert_gate_shard_evidence_refused(lambda: shard_plan.distribute_gate_shard_nodeids(
    shard_guard_incomplete_collection, shard_guard_manifest))
shard_guard_zero_manifest = copy.deepcopy(shard_guard_manifest)
shard_guard_zero_manifest["durations"] = dict.fromkeys(shard_guard_nodeids, SHARD_GUARD_ZERO)
assert all(shard_plan.distribute_gate_shard_nodeids(shard_guard_nodeids, shard_guard_zero_manifest))

with tempfile.TemporaryDirectory() as shard_guard_temporary:
    shard_guard_root = Path(shard_guard_temporary)
    shard_guard_path = shard_guard_root / shard_plan.GATE_SHARD_MANIFEST_FILENAME
    shard_guard_path.write_text(json.dumps(shard_guard_manifest))
    assert shard_plan.read_gate_shard_manifest(shard_guard_path) == shard_guard_manifest
    shard_guard_duplicate = json.dumps(shard_guard_manifest)
    shard_guard_path.write_text(shard_guard_duplicate.replace(
        '"schema_version":', f'"schema_version": {shard_plan.GATE_SHARD_SCHEMA_VERSION}, "schema_version":'))
    assert_gate_shard_evidence_refused(lambda: shard_plan.read_gate_shard_manifest(shard_guard_path))
    shard_guard_path.write_text(json.dumps(shard_guard_manifest))
    for shard_guard_index, shard_guard_members in enumerate(shard_guard_partitions):
        shard_guard_directory = shard_guard_root / (SHARD_GUARD_ARTIFACT_PREFIX + str(shard_guard_index))
        shard_guard_directory.mkdir()
        emit_gate_shard_guard_junit(shard_guard_directory / shard_plan.GATE_SHARD_REPORT_PATTERN.format(
            index=shard_guard_index), shard_guard_members)
        emit_gate_shard_guard_coverage(shard_guard_directory)
    # Artifact verifier root must contain only artifact dirs, just like download-artifact.
    shard_guard_path.unlink()
    shard_plan.verify_gate_shard_artifacts(shard_guard_manifest, shard_guard_root, SHARD_GUARD_ARTIFACT_PREFIX)
    shard_guard_first = shard_guard_root / (SHARD_GUARD_ARTIFACT_PREFIX + str(SHARD_GUARD_ZERO))
    shard_guard_coverage = shard_guard_first / SHARD_GUARD_COVERAGE_FILENAME
    shard_guard_coverage.unlink()
    assert_gate_shard_evidence_refused(lambda: shard_plan.verify_gate_shard_artifacts(
        shard_guard_manifest, shard_guard_root, SHARD_GUARD_ARTIFACT_PREFIX))
    emit_gate_shard_guard_coverage(shard_guard_first)
    shard_guard_coverage.write_bytes(SHARD_GUARD_CORRUPT_COVERAGE)
    assert_gate_shard_evidence_refused(lambda: shard_plan.verify_gate_shard_artifacts(
        shard_guard_manifest, shard_guard_root, SHARD_GUARD_ARTIFACT_PREFIX))
    assert_gate_shard_evidence_refused(lambda: shard_plan.snapshot_gate_shard_coverage(shard_guard_first))
    shard_guard_coverage.unlink()
    emit_gate_shard_guard_coverage(shard_guard_first)
    shard_guard_report = shard_guard_first / shard_plan.GATE_SHARD_REPORT_PATTERN.format(index=SHARD_GUARD_ZERO)
    shard_guard_report_tree = ET.parse(shard_guard_report)
    shard_guard_report_tree.getroot().set("tests", str(SHARD_GUARD_ZERO))
    shard_guard_report_tree.write(shard_guard_report)
    assert_gate_shard_evidence_refused(lambda: shard_plan.verify_gate_shard_artifacts(
        shard_guard_manifest, shard_guard_root, SHARD_GUARD_ARTIFACT_PREFIX))
    emit_gate_shard_guard_junit(shard_guard_report, shard_guard_partitions[SHARD_GUARD_ZERO])
    shard_plan.verify_gate_shard_artifacts(shard_guard_manifest, shard_guard_root, SHARD_GUARD_ARTIFACT_PREFIX)

    shard_guard_spec = importlib.util.spec_from_file_location(SHARD_GUARD_CONFTEST_MODULE,
        Path(__file__).with_name("conftest.py"))
    shard_guard_conftest = importlib.util.module_from_spec(shard_guard_spec)
    shard_guard_spec.loader.exec_module(shard_guard_conftest)
    shard_guard_config = SimpleNamespace(hook=SimpleNamespace(pytest_deselected=lambda **kwargs: None))
    shard_guard_path.write_text(json.dumps(shard_guard_manifest))
    with patch.dict(os.environ, {shard_plan.GATE_SHARD_INDEX_ENV: str(SHARD_GUARD_ZERO),
                               shard_plan.GATE_SHARD_TIMINGS_ENV: str(shard_guard_path)}):
        shard_guard_items = [SimpleNamespace(nodeid=nodeid) for nodeid in shard_guard_nodeids]
        shard_guard_conftest.pytest_collection_modifyitems(shard_guard_config, shard_guard_items)
        assert {item.nodeid for item in shard_guard_items} == set(shard_guard_partitions[SHARD_GUARD_ZERO])
        # Mutation control: reintroduce a dropped-case planner at the actual integration seam.
        shard_guard_mutation = copy.deepcopy(shard_guard_partitions)
        shard_guard_mutation[SHARD_GUARD_ZERO].pop()
        with patch.object(shard_guard_conftest, "distribute_gate_shard_nodeids", return_value=shard_guard_mutation):
            assert_gate_shard_evidence_refused(lambda: shard_guard_conftest.pytest_collection_modifyitems(
                shard_guard_config, [SimpleNamespace(nodeid=nodeid) for nodeid in shard_guard_nodeids]), pytest.UsageError)
        print("MUTATION FAIL expected: dropped-case planner refused by actual collection hook")
        shard_guard_items = [SimpleNamespace(nodeid=nodeid) for nodeid in shard_guard_nodeids]
        shard_guard_conftest.pytest_collection_modifyitems(shard_guard_config, shard_guard_items)
        assert {item.nodeid for item in shard_guard_items} == set(shard_guard_partitions[SHARD_GUARD_ZERO])
        print("RESTORED PASS: complete exact-once shard selection")

print("Gate shard complete inventory, slow-case faults, artifact gaps and mutation proof passed")
