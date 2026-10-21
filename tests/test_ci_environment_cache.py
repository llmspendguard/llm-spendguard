"""Parse the fixed YAML execution contract; unused scalar text is never evidence."""
from copy import deepcopy
from pathlib import Path

import yaml

CI_ENV_WORKFLOW = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "ci.yml"
CI_CACHE_INSTALL_JOBS = ("test", "coverage")
CI_CACHE_PYTHON_VERSIONS = ["3.11", "3.12"]
CI_CACHE_REQUIRED_ENV = {"CI_TEST_EXTRAS": ".[openai,anthropic,otel,chat,dev]", "CI_PYDANTIC_FLOOR": "pydantic>=2"}
CI_CACHE_EXPECTED_COUNT = 1
CI_CACHE_INSTALL_LINES = (
    'if [ ! -x "${CI_TEST_VENV}/bin/python" ]; then',
    'uv venv --python "${{ steps.python.outputs.python-path }}" "$CI_TEST_VENV"',
    'fi',
    'echo "${PWD}/${CI_TEST_VENV}/bin" >> "$GITHUB_PATH"',
    'uv pip install --python "${CI_TEST_VENV}/bin/python" -e "$CI_TEST_EXTRAS" "$CI_PYDANTIC_FLOOR"',
)
CI_CACHE_ACTION_INPUTS = {
    "astral-sh/setup-uv": {"enable-cache": "true", "cache-dependency-glob": "pyproject.toml"},
    "actions/cache": {
        "path": "${{ env.CI_TEST_VENV }}\n${{ env.TIKTOKEN_CACHE_DIR }}\n",
        "key": "${{ env.CI_TEST_CACHE_REVISION }}-${{ runner.os }}-${{ runner.arch }}-${{ steps.python.outputs.python-version }}-${{ hashFiles('pyproject.toml', '.github/workflows/ci.yml') }}",
    },
    "actions/setup-python": {"python-version": "${{ matrix.python-version }}"},
}
CI_CACHE_DISABLED_FIELDS = ("if", "continue-on-error", "defaults", "shell", "working-directory")
CI_CACHE_NOOP_COMMAND = "true"
CI_CACHE_UNUSED_FIELD = "x-unused-contract-evidence"


def parse_ci_cache_run_lines(run_text):
    """Parse blank/comment lines from the fixed shell command format."""
    return tuple(line.strip() for line in run_text.splitlines()
                 if line.strip() and not line.lstrip().startswith("#"))


def verify_ci_cache_active_node(node):
    assert not set(CI_CACHE_DISABLED_FIELDS).intersection(node), "Conditional/overridden CI execution"
    assert not node.get("env"), "CI environment shadowing"


def verify_ci_environment_cache_contract(workflow_text):
    # BaseLoader preserves GitHub scalar spellings, including the key 'on'.
    workflow = yaml.load(workflow_text, Loader=yaml.BaseLoader)
    assert not set(CI_CACHE_DISABLED_FIELDS).intersection(workflow)
    for key, value in CI_CACHE_REQUIRED_ENV.items():
        assert workflow["env"][key] == value
    assert workflow["env"]["CI_TEST_VENV"]
    assert workflow["env"]["TIKTOKEN_CACHE_DIR"]
    for job_name in CI_CACHE_INSTALL_JOBS:
        job = workflow["jobs"][job_name]
        assert not set(CI_CACHE_DISABLED_FIELDS).intersection(job)
        assert not set(workflow["env"]).intersection(job.get("env", {}))
        assert job["strategy"]["matrix"]["python-version"] == CI_CACHE_PYTHON_VERSIONS
        assert not set(("include", "exclude")).intersection(job["strategy"]["matrix"])
        steps = job["steps"]
        install_steps = [step for step in steps if "run" in step
                         and parse_ci_cache_run_lines(step["run"]) == CI_CACHE_INSTALL_LINES]
        assert len(install_steps) == CI_CACHE_EXPECTED_COUNT
        verify_ci_cache_active_node(install_steps[0])
        for action, required_inputs in CI_CACHE_ACTION_INPUTS.items():
            action_steps = [step for step in steps if step.get("uses", "").partition("@")[0] == action]
            assert len(action_steps) == CI_CACHE_EXPECTED_COUNT
            action_step = action_steps[0]
            verify_ci_cache_active_node(action_step)
            assert action_step["uses"].partition("@")[2]
            assert action_step["with"] == required_inputs
            assert steps.index(action_step) < steps.index(install_steps[0])
            if action == "actions/setup-python":
                assert action_step["id"] == "python"


ci_cache_original_text = CI_ENV_WORKFLOW.read_text()
verify_ci_environment_cache_contract(ci_cache_original_text)
# Preserve every old fragment verbatim while moving one required command out of execution.
ci_cache_unused_install_text = ci_cache_original_text.replace(
    CI_CACHE_INSTALL_LINES[-1], CI_CACHE_NOOP_COMMAND, CI_CACHE_EXPECTED_COUNT
) + f"\n{CI_CACHE_UNUSED_FIELD}: |\n  {CI_CACHE_INSTALL_LINES[-1]}\n"
try:
    verify_ci_environment_cache_contract(ci_cache_unused_install_text)
except AssertionError:
    pass
else:
    raise AssertionError("Install command in an unused YAML scalar was accepted")
ci_cache_configuration = yaml.load(ci_cache_original_text, Loader=yaml.BaseLoader)
ci_cache_faults = []
for ci_cache_job_name in CI_CACHE_INSTALL_JOBS:
    ci_cache_steps = ci_cache_configuration["jobs"][ci_cache_job_name]["steps"]
    for ci_cache_step_index, ci_cache_step in enumerate(ci_cache_steps):
        if ("run" in ci_cache_step and parse_ci_cache_run_lines(ci_cache_step["run"]) == CI_CACHE_INSTALL_LINES
                or ci_cache_step.get("uses", "").partition("@")[0] in CI_CACHE_ACTION_INPUTS):
            for ci_cache_disabled_field in ("if", "continue-on-error", "env"):
                ci_cache_fault = deepcopy(ci_cache_configuration)
                ci_cache_fault["jobs"][ci_cache_job_name]["steps"][ci_cache_step_index][ci_cache_disabled_field] = (
                    {"CI_PYDANTIC_FLOOR": ""} if ci_cache_disabled_field == "env" else "false")
                ci_cache_faults.append(ci_cache_fault)
            ci_cache_fault = deepcopy(ci_cache_configuration)
            ci_cache_moved_step = ci_cache_fault["jobs"][ci_cache_job_name]["steps"][ci_cache_step_index]
            ci_cache_fault[CI_CACHE_UNUSED_FIELD] = deepcopy(ci_cache_moved_step)
            ci_cache_moved_step.clear()
            ci_cache_moved_step["run"] = CI_CACHE_NOOP_COMMAND
            ci_cache_faults.append(ci_cache_fault)
for ci_cache_fault in ci_cache_faults:
    try:
        verify_ci_environment_cache_contract(yaml.dump(ci_cache_fault))
    except (AssertionError, KeyError):
        pass
    else:
        raise AssertionError("Disabled or unused cache/install contract was accepted")
print(f"CI YAML execution contract and {len(ci_cache_faults)} injected faults pass")
