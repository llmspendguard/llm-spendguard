"""Offline target identity, bounded interpreter probe and host-doctor failure proof."""
import json
import os
import subprocess
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault("SPENDGUARD_TEST_ISOLATED", "1")
from spendguard import distribution as dist

TARGET_FIXTURE = {"sha": "blessed-fixture-sha", "version": "fixture-version"}
POLICY_FIXTURE = {"probe_timeout_s": 3.5, "install_timeout_s": 7.5}
GOOD_PROBE_FIXTURE = {"confirmed": True, "version": TARGET_FIXTURE["version"],
                      "metadata_version": TARGET_FIXTURE["version"], "sha": TARGET_FIXTURE["sha"],
                      "dirty": False, "gate_enforcing": True}


def test_distribution_target_uses_pointer_commit_version_and_rejects_missing_or_conflicting_pointer():
    with patch.object(dist.release, "green_pointer", return_value={"sha": TARGET_FIXTURE["sha"]}), patch.object(
            dist.release, "_git_src", return_value=f"__version__ = {TARGET_FIXTURE['version']!r}") as git_source:
        target = dist.resolve_distribution_target()
        assert target["version"] == TARGET_FIXTURE["version"] and target["sha"] == TARGET_FIXTURE["sha"]
        assert git_source.call_args.args[0] == "show"
        assert git_source.call_args.args[1].split(":", 1)[0] == TARGET_FIXTURE["sha"]
    for pointer in (None, {"sha": TARGET_FIXTURE["sha"], "version": "conflicting-fixture-version"}):
        with patch.object(dist.release, "green_pointer", return_value=pointer), patch.object(
                dist.release, "_git_src", return_value=f"__version__ = {TARGET_FIXTURE['version']!r}"):
            try:
                dist.resolve_distribution_target()
            except ValueError:
                continue
            raise AssertionError(f"invalid deployed pointer accepted: {pointer}")


def test_distribution_interpreter_probe_is_bounded_and_identity_checks_are_load_bearing():
    def fixture_interpreter_subprocess(arguments, **options):
        assert options["timeout"] == POLICY_FIXTURE["probe_timeout_s"]
        assert arguments[1:] == ["-c", dist.DISTRIBUTION_INTERPRETER_PROBE]
        assert "PYTHONPATH" not in options["env"]
        return SimpleNamespace(returncode=0, stdout=json.dumps(GOOD_PROBE_FIXTURE), stderr="")

    with patch.object(dist.subprocess, "run", fixture_interpreter_subprocess):
        probe = dist.probe_distribution_interpreter("fixture-python", POLICY_FIXTURE)
    assert dist.distribution_interpreter_on_target(probe, TARGET_FIXTURE)
    for field, wrong_value in (("confirmed", False), ("version", "old-import"),
                               ("metadata_version", "old-metadata"), ("sha", "old-sha"), ("dirty", True)):
        assert not dist.distribution_interpreter_on_target({**probe, field: wrong_value}, TARGET_FIXTURE), field
    with patch.object(dist.subprocess, "run", side_effect=subprocess.TimeoutExpired("fixture-python", POLICY_FIXTURE["probe_timeout_s"])):
        assert not dist.probe_distribution_interpreter("fixture-python", POLICY_FIXTURE)["confirmed"]


def assert_distribution_host_doctor_red_then_green():
    for return_code in (1, 0):
        def fixture_host_doctor(arguments, fixture_return_code=return_code, **options):
            assert arguments[1:] == ["-m", "spendguard", "doctor"]
            assert options["timeout"] == POLICY_FIXTURE["probe_timeout_s"]
            return SimpleNamespace(returncode=fixture_return_code, stdout="fixture doctor output", stderr="")

        with patch.object(dist, "probe_distribution_interpreter", return_value=GOOD_PROBE_FIXTURE), patch.object(
                dist.subprocess, "run", fixture_host_doctor):
            row = dist.attest_distribution_host(TARGET_FIXTURE, POLICY_FIXTURE)
        assert row["healthy"] is (return_code == 0)
        assert bool(row["straggler_reason"]) is (return_code != 0)


def test_distribution_doctor_check_removed_makes_red_green_assertion_fail():
    original_attestation = dist.attest_distribution_host

    def ignore_fixture_doctor_exit_code(*arguments, **keywords):
        row = original_attestation(*arguments, **keywords)
        return {**row, "healthy": True, "straggler_reason": None}

    with patch.object(dist, "attest_distribution_host", ignore_fixture_doctor_exit_code):
        try:
            assert_distribution_host_doctor_red_then_green()
        except AssertionError:
            return
    raise AssertionError("host doctor mutation escaped the red assertion")


def test_distribution_discovery_failure_retains_host_and_daemon_attestation():
    healthy_host = dist.distribution_surface_row("fixture-host", "host", True, True)
    healthy_daemon = dist.distribution_surface_row("fixture-daemon", "mcp", True, True, fd_ok=True)
    with patch.object(dist, "resolve_distribution_target", return_value=TARGET_FIXTURE), patch.object(
            dist, "distribution_policy", return_value=POLICY_FIXTURE), patch.object(
            dist.consumer_audit, "audit_consumer_workspaces", side_effect=OSError("fixture audit unavailable")), patch.object(
            dist.setup, "discover_distribution_interpreters", side_effect=OSError("fixture discovery unavailable")), patch.object(
            dist, "attest_distribution_api_consumers", return_value=[]), patch.object(
            dist, "attest_distribution_daemons", return_value=[healthy_daemon]) as daemon, patch.object(
            dist, "attest_distribution_host", return_value=healthy_host) as host:
        result = dist.distribute_fleet(check=True, settings={})
    assert result["exit_code"] != 0
    assert set(result["summary"]["stragglers"]) == {"consumer-audit", "interpreter-discovery"}
    assert {row["surface"] for row in result["surfaces"]} >= {"fixture-host", "fixture-daemon"}
    daemon.assert_called_once()
    host.assert_called_once()


def test_distribution_interpreter_gate_proof_red_then_green():
    healthy_host = dist.distribution_surface_row("fixture-host", "host", True, True)
    inventory = {"interpreters": [{"python": "fixture-python", "venv": None, "kind": "framework-python"}],
                 "gaps": []}
    for enforcing in (False, True):
        with patch.object(dist, "resolve_distribution_target", return_value=TARGET_FIXTURE), patch.object(
                dist, "distribution_policy", return_value=POLICY_FIXTURE), patch.object(
                dist.consumer_audit, "audit_consumer_workspaces", return_value={"consumers": [], "pruned_at_depth": []}), patch.object(
                dist.setup, "discover_distribution_interpreters", return_value=inventory), patch.object(
                dist, "probe_distribution_interpreter", return_value={**GOOD_PROBE_FIXTURE, "gate_enforcing": enforcing}), patch.object(
                dist, "attest_distribution_api_consumers", return_value=[]), patch.object(
                dist, "attest_distribution_daemons", return_value=[]), patch.object(
                dist, "attest_distribution_host", return_value=healthy_host):
            result = dist.distribute_fleet(check=True, settings={})
        assert bool(result["exit_code"]) is (not enforcing)
        interpreter_rows = [row for row in result["surfaces"] if row["kind"] == "framework-python"]
        assert len(interpreter_rows) == 1 and interpreter_rows[0]["healthy"] is enforcing


test_distribution_target_uses_pointer_commit_version_and_rejects_missing_or_conflicting_pointer()
test_distribution_interpreter_probe_is_bounded_and_identity_checks_are_load_bearing()
assert_distribution_host_doctor_red_then_green()
test_distribution_doctor_check_removed_makes_red_green_assertion_fail()
test_distribution_discovery_failure_retains_host_and_daemon_attestation()
test_distribution_interpreter_gate_proof_red_then_green()
