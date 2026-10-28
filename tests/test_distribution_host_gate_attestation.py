"""Offline host proof through the real distribute CLI, with a gate mutation kill."""
import contextlib
from datetime import datetime, timezone
import inspect
import io
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault("SPENDGUARD_NO_AUTOINSTALL", "1")
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from spendguard import distribution, manifest  # noqa: E402

HOST_GATE_TARGET = {"sha": "host-gate-fixture", "version": "host-gate-fixture"}
HOST_GATE_DOCTOR_OK = 0
HOST_GATE_DOCTOR_FAILED = 1
HOST_GATE_DOCTOR_STDOUT = "host doctor fixture stdout\n"
HOST_GATE_DOCTOR_STDERR = "host doctor fixture stderr\n"


@contextlib.contextmanager
def distribution_host_gate_fixture(proof, doctor_code=HOST_GATE_DOCTOR_OK):
    verified_at = datetime.now(timezone.utc).isoformat()
    persisted_manifest = {
        "schema_version": manifest.MANIFEST_SCHEMA_VERSION,
        "writer_version": manifest.SPENDGUARD_WRITER_VERSION,
        "last_verified_at": verified_at,
        "enumeration_root_mtimes": manifest.manifest_scan_root_mtimes(()),
        "surfaces": [],
        "gaps": [],
    }
    with contextlib.ExitStack() as stack:
        for attribute, value in (
            ("resolve_distribution_target", HOST_GATE_TARGET),
            ("distribution_policy", {"probe_timeout_s": distribution.DISTRIBUTION_PROBE_TIMEOUT_S}),
            ("probe_distribution_interpreter", proof),
            ("attest_distribution_api_consumers", []),
            ("attest_distribution_daemons", []),
        ):
            stack.enter_context(patch.object(distribution, attribute, return_value=value))
        stack.enter_context(patch.object(distribution.config, "consumer_audit_settings", return_value={}))
        stack.enter_context(patch.object(distribution.consumer_audit, "audit_consumer_workspaces",
                                         return_value={"consumers": [], "pruned_at_depth": []}))
        stack.enter_context(patch.object(distribution.setup, "discover_distribution_interpreters",
                                         return_value={"interpreters": [], "gaps": []}))
        stack.enter_context(patch.object(manifest, "manifest_path",
                                         return_value=SimpleNamespace(is_file=lambda: True)))
        stack.enter_context(patch.object(manifest, "read_surface_manifest", return_value=persisted_manifest))
        doctor = stack.enter_context(patch.object(distribution.subprocess, "run", return_value=SimpleNamespace(
            returncode=doctor_code, stdout=HOST_GATE_DOCTOR_STDOUT, stderr=HOST_GATE_DOCTOR_STDERR)))
        yield doctor


def distribution_host_gate_cli_result():
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        code = distribution.distribute_command(["--check", "--json"])
    result = json.loads(output.getvalue())
    assert code == result["exit_code"]
    return code, result, result["surfaces"][0]


def test_distribution_host_requires_confirmed_enforcing_gate():
    armed = dict(confirmed=True, gate_enforcing=True, dirty=False,
                 version=HOST_GATE_TARGET["version"], metadata_version=HOST_GATE_TARGET["version"],
                 sha=HOST_GATE_TARGET["sha"])
    cases = (
        (armed, HOST_GATE_DOCTOR_OK, True),
        (dict(armed, gate_enforcing=False), HOST_GATE_DOCTOR_OK, False),
        ({key: value for key, value in armed.items() if key != "gate_enforcing"}, HOST_GATE_DOCTOR_OK, False),
        (dict(armed, confirmed=False), HOST_GATE_DOCTOR_OK, False),
        ({key: value for key, value in armed.items() if key != "confirmed"}, HOST_GATE_DOCTOR_OK, False),
        (armed, HOST_GATE_DOCTOR_FAILED, False),
    )
    for proof, doctor_code, healthy in cases:
        with distribution_host_gate_fixture(proof, doctor_code) as doctor:
            code, result, host = distribution_host_gate_cli_result()
            assert host["healthy"] is healthy, host
            assert host["surface"] == host["kind"] == "host"
            assert host["probe"] == proof
            assert host["doctor_exit_code"] == doctor_code
            assert host["doctor_output"] == HOST_GATE_DOCTOR_STDOUT + HOST_GATE_DOCTOR_STDERR
            assert host["straggler_reason"] is None if healthy else bool(host["straggler_reason"])
            if not proof.get("confirmed"):
                assert host["straggler_reason"] == "host gate proof unconfirmed"
            elif not proof.get("gate_enforcing"):
                assert host["straggler_reason"] == "host gate not enforcing"
            assert result["summary"]["stragglers"] == ([] if healthy else ["host"])
            assert (code == 0) is healthy
            doctor_calls = [call for call in doctor.call_args_list
                            if call.args[0] == [sys.executable, "-m", "spendguard", "doctor"]]
            assert len(doctor_calls) == 1


def test_distribution_host_enforcement_guard_is_load_bearing():
    original = distribution.attest_distribution_host
    source = inspect.getsource(original)
    requirement = ' and probe.get("gate_enforcing")'
    assert requirement in source
    namespace = dict(vars(distribution))
    exec(compile(source.replace(requirement, ""), inspect.getfile(original), "exec"), namespace)
    with patch.object(distribution, "attest_distribution_host", namespace[original.__name__]):
        try:
            test_distribution_host_requires_confirmed_enforcing_gate()
        except AssertionError:
            print("MUTATION RED: unarmed host without gate_enforcing requirement")
        else:
            raise AssertionError("host gate_enforcing removal survived")


if __name__ == "__main__":
    test_distribution_host_requires_confirmed_enforcing_gate()
    test_distribution_host_enforcement_guard_is_load_bearing()
    print("PASS host gate proof: armed green; false/missing proof and doctor failure red; evidence retained")
