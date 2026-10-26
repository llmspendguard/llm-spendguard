"""Doctor must report the startup behavior of bare Python, not merely its own imported process."""
import contextlib
import io
import os
import sys
import tempfile
from unittest.mock import patch


if not os.environ.get("SPENDGUARD_TEST_ISOLATED"):
    os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"
    os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="spendguard-doctor-startup-")
    os.execv(sys.executable, [sys.executable] + sys.argv)

from spendguard import gate, setup  # noqa: E402


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


test_doctor_refuses_yes_when_bare_python_startup_gate_is_inactive()
test_doctor_reports_yes_when_process_and_startup_gate_are_active()
print("[OK] doctor binds its verdict to bare-Python startup gating")
