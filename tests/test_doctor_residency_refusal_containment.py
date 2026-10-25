"""Expected bounded residency refusals degrade doctor without weakening ledger reads."""
import contextlib
import io
import logging
import os
from pathlib import Path
from unittest.mock import patch

from _pytest_fixture_bridge import align_test_startup_home

DOCTOR_RESIDENCY_REFUSAL_HOME_PREFIX = "doctor-residency-refusal-"
align_test_startup_home(DOCTOR_RESIDENCY_REFUSAL_HOME_PREFIX)

import spendguard
from spendguard import budget, calls, claudecode, config, gate, ledger_read, setup

DOCTOR_RESIDENCY_TIMEOUT_LINE = "residency : unavailable (ledger scan exceeded its time budget)"
DOCTOR_RESIDENCY_SCHEMA_REASON = "injected missing residency schema"
DOCTOR_RESIDENCY_SCHEMA_LINE = (
    f"residency : unavailable (LedgerReadSchemaError: {DOCTOR_RESIDENCY_SCHEMA_REASON})")
DOCTOR_RESIDENCY_CONTINUATION_LINE = "delegation lanes ready: injected continuation"
DOCTOR_RESIDENCY_TRACEBACK_MARKER = "Traceback (most recent call last)"


def run_doctor_residency_refusal_containment():
    spendguard.require()
    assert config.HOME == Path(os.environ["SPENDGUARD_HOME"])
    assert Path(config.db_path()).parent == config.HOME
    budget._ledger()  # Initialize only the isolated fixture's schema before doctor status reads.
    calls._calls_db()
    refusal_cases = (
        (ledger_read.LedgerReadTimeout(ledger_read.DEFAULT_LEDGER_READ_TIME_BUDGET_S),
         DOCTOR_RESIDENCY_TIMEOUT_LINE),
        (ledger_read.LedgerReadSchemaError(DOCTOR_RESIDENCY_SCHEMA_REASON),
         DOCTOR_RESIDENCY_SCHEMA_LINE),
    )
    for refusal, expected_line in refusal_cases:
        @contextlib.contextmanager
        def inject_doctor_residency_connection_refusal(path=None, refusal_to_raise=refusal):
            assert path is None or Path(path).parent == config.HOME
            raise refusal_to_raise
            yield  # The injected failure happens at context entry, before any ledger can open.

        doctor_output = io.StringIO()
        doctor_errors = io.StringIO()
        refusal_logs = io.StringIO()
        refusal_handler = logging.StreamHandler(refusal_logs)
        refusal_logger = logging.getLogger("spendguard")
        refusal_logger.addHandler(refusal_handler)
        try:
            with patch.object(ledger_read, "_ledger_read_connection",
                              inject_doctor_residency_connection_refusal):
                try:
                    ledger_read.read_cc_residency_totals()
                except ledger_read.LedgerReadError as caught_refusal:
                    assert caught_refusal is refusal, "containment refusal must retain its typed identity"
                else:
                    raise AssertionError("bounded read must refuse instead of returning partial totals")
            # Fault only the optional axis during the full doctor invocation; other ledger
            # consumers retain their real isolated fixture connection and containment.
            with patch.object(ledger_read, "read_cc_residency_totals", side_effect=refusal), \
                    patch.object(setup, "_startup_gate_active", return_value=True), \
                    patch("spendguard.delegate_router.delegation_doctor_line",
                          return_value=DOCTOR_RESIDENCY_CONTINUATION_LINE), \
                    contextlib.redirect_stdout(doctor_output), \
                    contextlib.redirect_stderr(doctor_errors):
                assert claudecode.residency_summary_lines() == [expected_line]
                assert gate._cli("doctor") == 0
        finally:
            refusal_logger.removeHandler(refusal_handler)
        rendered_doctor = doctor_output.getvalue()
        assert expected_line in rendered_doctor
        assert DOCTOR_RESIDENCY_CONTINUATION_LINE in rendered_doctor
        assert DOCTOR_RESIDENCY_TRACEBACK_MARKER not in rendered_doctor
        assert DOCTOR_RESIDENCY_TRACEBACK_MARKER not in doctor_errors.getvalue()
        assert not refusal_logs.getvalue(), refusal_logs.getvalue()
    print("Doctor residency refusals: typed fail-closed reads; named status gaps; continuation and exit zero")


if __name__ == "__main__":
    run_doctor_residency_refusal_containment()
