"""Doctor discloses the effective accounting and cross-process cap mode, offline."""
import contextlib
import io
from unittest.mock import patch

from spendguard import config, gate


BUDGET_MODE_EXPECTATIONS = (
    ("memory", "process-local, NON-DURABLE", "cross-process cap + durable spend-events require budget_backend=sqlite"),
    ("sqlite", "durable, cross-process", None),
)

with patch.object(gate, "install"), patch.object(gate, "_stale_editable_install", return_value=(False, None, None)):
    for doctor_backend, doctor_description, doctor_remediation in BUDGET_MODE_EXPECTATIONS:
        # Exercise the real accessor, including the configured memory mode, with isolated configuration.
        with patch.object(config, "_cfg_get", side_effect=lambda section, key, default=None, backend=doctor_backend: (
            backend if (section, key) == ("budget", "backend") else default
        )):
            doctor_capture = io.StringIO()
            with contextlib.redirect_stdout(doctor_capture):
                gate._cli("doctor")
        doctor_output = doctor_capture.getvalue()
        assert f"budget enforcement: {doctor_description} (budget_backend={doctor_backend})" in doctor_output, doctor_output
        assert doctor_output.count("budget enforcement:") == 1, doctor_output
        if doctor_remediation:
            assert doctor_remediation in doctor_output, doctor_output

print("OK test_doctor_discloses_budget_mode")
