"""The effort readiness declaration runs real offline checks and fails closed."""
from pathlib import Path
import tempfile

from spendguard.capability_readiness import load_capability_readiness_manifest
from spendguard.capability_readiness_gates import run_capability_script_readiness_gate

EFFORT_GATE_CAPABILITY_ID = "effort-learn-loop"
EFFORT_GATE_CALLABLE = "spendguard.capability_readiness_gates:run_capability_script_readiness_gate"
EFFORT_GATE_REQUIRED_SCRIPTS = {
    "tests/test_effort_on_frontier.py",
    "tests/test_best_value_select.py",
    "tests/test_bakeoff_effort_sweep.py",
    "tests/test_effort_titration.py",
    "tests/test_lane_records_applied_effort.py",
    "tests/test_panel_effort_autoapply.py",
}
EFFORT_GATE_FAILURE_SOURCE = "raise SystemExit(1)\n"
EFFORT_GATE_FAILURE_FILENAME = "effort_gate_deliberate_failure.py"


def verify_effort_learn_loop_manifest_and_real_gate():
    """Require every reviewed check, execute them, then inject a failing check."""
    repo_root = Path(__file__).resolve().parents[1]
    _, items = load_capability_readiness_manifest()
    effort_item = next(item for item in items if item.id == EFFORT_GATE_CAPABILITY_ID)
    assert effort_item.gate == EFFORT_GATE_CALLABLE
    scripts = effort_item.arguments["scripts"]
    assert set(scripts) == EFFORT_GATE_REQUIRED_SCRIPTS
    assert len(scripts) == len(EFFORT_GATE_REQUIRED_SCRIPTS)
    assert all((repo_root / script).is_file() for script in scripts)
    context = {"repo_root": repo_root}
    passing = run_capability_script_readiness_gate(context=context, **effort_item.arguments)
    assert passing.status == "PASS", passing
    assert len(passing.evidence) == len(scripts)
    with tempfile.TemporaryDirectory() as scratch_directory:
        failing_script = Path(scratch_directory) / EFFORT_GATE_FAILURE_FILENAME
        failing_script.write_text(EFFORT_GATE_FAILURE_SOURCE, encoding="utf-8")
        failing = run_capability_script_readiness_gate(
            context=context, scripts=[*scripts, str(failing_script)])
    assert failing.status == "FAIL", failing
    assert len(failing.evidence) == len(scripts) + 1
    assert EFFORT_GATE_FAILURE_FILENAME in failing.detail


if __name__ == "__main__":
    verify_effort_learn_loop_manifest_and_real_gate()
    print("effort learn-loop readiness: real checks PASS; injected failure FAIL")
