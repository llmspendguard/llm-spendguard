"""Doctor must report the startup behavior of bare Python, not merely its own imported process."""
import contextlib
import io
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch


if not os.environ.get("SPENDGUARD_TEST_ISOLATED"):
    os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"
    os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="spendguard-doctor-startup-")
    os.execv(sys.executable, [sys.executable] + sys.argv)

from spendguard import config, fleet, gate, setup  # noqa: E402

DOCTOR_FRESH_REGISTRY_DIRECTORY = "fresh-registry"


def _run_doctor_with_startup_probe_result(startup_gate_active):
    doctor_output = io.StringIO()
    with (
        patch.object(gate, "install"),
        patch.object(gate, "_enforcement_armed", return_value=True),
        patch.object(setup, "_startup_gate_active", return_value=startup_gate_active),
        contextlib.redirect_stdout(doctor_output),
    ):
        return gate._cli("doctor"), doctor_output.getvalue()


def test_doctor_refuses_yes_when_bare_python_startup_gate_is_inactive():
    doctor_exit_code, doctor_output = _run_doctor_with_startup_probe_result(False)

    assert gate.ENFORCING_YES not in doctor_output
    assert f"{gate.ENFORCING_MARKER}:" in doctor_output
    assert gate.ENFORCING_NO in doctor_output
    assert "startup does NOT auto-load the gate" in doctor_output
    assert "bare `python`" in doctor_output
    assert doctor_exit_code != 0


def test_doctor_reports_yes_when_process_and_startup_gate_are_active():
    doctor_exit_code, doctor_output = _run_doctor_with_startup_probe_result(True)

    assert f"{gate.ENFORCING_MARKER}:" in doctor_output
    assert gate.ENFORCING_YES in doctor_output
    assert doctor_exit_code == 0


def test_doctor_registration_initializes_absent_state_at_write_seam():
    with tempfile.TemporaryDirectory() as temporary:
        fresh_state = Path(temporary) / DOCTOR_FRESH_REGISTRY_DIRECTORY
        with patch.object(config, "HOME", fresh_state), patch.object(fleet, "_registration_failure", None):
            fleet._fleet_db(read_only=True).close()
            assert not fresh_state.exists(), "read-only fleet access must not create state"
            assert fleet.register_enforcing_interpreter(), fleet._registration_failure
            assert fresh_state.is_dir()
            assert fleet.enforcing_interpreter_registration_status()["registered"]
            doctor_exit_code, doctor_output = _run_doctor_with_startup_probe_result(True)
            assert doctor_exit_code == 0, doctor_output


test_doctor_refuses_yes_when_bare_python_startup_gate_is_inactive()
test_doctor_reports_yes_when_process_and_startup_gate_are_active()
test_doctor_registration_initializes_absent_state_at_write_seam()
print("[OK] doctor binds its verdict to bare-Python startup gating")
