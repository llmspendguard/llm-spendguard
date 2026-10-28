"""Offline proofs for cached wheels, dev preservation, and kind-aware attestation."""
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from spendguard import distribution, manifest

WHEEL_FIXTURE_VERSION = "8.7.6"
WHEEL_FIXTURE_OLD_VERSION = "7.6.5"
WHEEL_FIXTURE_SHA = "fixture-green-sha"
WHEEL_FIXTURE_STALE_SHA = "fixture-stale-sha"
WHEEL_FIXTURE_POLICY = {"probe_timeout_s": 2.0, "install_timeout_s": 3.0}


def wheel_fixture_target(root):
    return {"sha": WHEEL_FIXTURE_SHA, "version": WHEEL_FIXTURE_VERSION,
            "green_version": WHEEL_FIXTURE_VERSION, "checkout": str(root)}


def test_distribution_wheel_build_cache_and_version_cross_check(tmp_path):
    target = wheel_fixture_target(tmp_path)
    wheel_name = f"llm_spendguard-{WHEEL_FIXTURE_VERSION}-py3-none-any.whl"

    def fixture_build(command, **options):
        assert "--no-deps" in command and "--no-build-isolation" in command
        (tmp_path / "state" / distribution.DISTRIBUTION_WHEELS_DIRECTORY / wheel_name).touch()
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    with patch.object(distribution.config, "HOME", tmp_path / "state"), patch.object(
            distribution.subprocess, "run", side_effect=fixture_build) as builder:
        first = distribution.ensure_distribution_wheel(target, tmp_path, WHEEL_FIXTURE_POLICY)
        second = distribution.ensure_distribution_wheel(target, tmp_path, WHEEL_FIXTURE_POLICY)
    assert first == second and builder.call_count == 1

    wrong_target = {**target, "version": WHEEL_FIXTURE_OLD_VERSION}
    with patch.object(distribution.config, "HOME", tmp_path / "state"):
        try:
            distribution.ensure_distribution_wheel(wrong_target, tmp_path, WHEEL_FIXTURE_POLICY)
        except FileNotFoundError as exc:
            assert WHEEL_FIXTURE_OLD_VERSION in str(exc)
        else:
            raise AssertionError("missing rollback wheel was silently accepted")


def test_distribution_consumer_uses_wheel_and_canonical_dev_stays_editable(tmp_path):
    dev_python = str(tmp_path / "dev" / "bin" / "python")
    consumer_python = str(tmp_path / "consumer" / "bin" / "python")
    inventory = {"interpreters": [
        {"python": dev_python, "venv": str(tmp_path / "dev"), "kind": "venv"},
        {"python": consumer_python, "venv": str(tmp_path / "consumer"), "kind": "venv"},
    ], "gaps": []}
    installs = []

    def fixture_kind(python, *_args, **_options):
        if python == dev_python:
            return {"install_kind": manifest.MANIFEST_EDITABLE_KIND, "wheel_version": None,
                    "editable_origin": str(tmp_path)}
        return {"install_kind": manifest.MANIFEST_WHEEL_KIND, "wheel_version": WHEEL_FIXTURE_VERSION,
                "editable_origin": None}

    healthy_probe = {"confirmed": True, "version": WHEEL_FIXTURE_VERSION,
                     "metadata_version": WHEEL_FIXTURE_VERSION, "sha": WHEEL_FIXTURE_SHA,
                     "dirty": False, "gate_enforcing": True}
    stale_probe = {**healthy_probe, "version": WHEEL_FIXTURE_OLD_VERSION,
                   "metadata_version": WHEEL_FIXTURE_OLD_VERSION, "sha": WHEEL_FIXTURE_STALE_SHA}

    def fixture_probe(python, _policy):
        install_kind = "editable" if python == dev_python else "wheel"
        return healthy_probe if any(event[0] == install_kind and event[1] == python for event in installs) else stale_probe
    target = wheel_fixture_target(tmp_path)
    healthy_host = distribution.distribution_surface_row("host", "host", True, True)
    with patch.object(distribution, "resolve_distribution_target", return_value=target), patch.object(
            distribution, "distribution_policy", return_value=WHEEL_FIXTURE_POLICY), patch.object(
            distribution.consumer_audit, "audit_consumer_workspaces",
            return_value={"consumers": [], "pruned_at_depth": []}), patch.object(
            distribution.setup, "discover_distribution_interpreters", return_value=inventory), patch.object(
            distribution, "inspect_distribution_install", side_effect=fixture_kind), patch.object(
            distribution, "prepare_distribution_checkout", return_value=str(tmp_path)), patch.object(
            distribution, "ensure_distribution_wheel"), patch.object(
            distribution.setup, "refresh_consumer_wheel_package",
            side_effect=lambda python, cache, version, timeout: installs.append(("wheel", python, version))), patch.object(
            distribution.fleet, "_refresh_editable_install",
            side_effect=lambda python, checkout, timeout: installs.append(("editable", python, checkout))), patch.object(
            distribution.setup, "install_hook", return_value=0), patch.object(
            distribution, "probe_distribution_interpreter", side_effect=fixture_probe), patch.object(
            distribution, "attest_distribution_api_consumers", return_value=[]), patch.object(
            distribution, "attest_distribution_daemons", return_value=[]), patch.object(
            distribution, "attest_distribution_host", return_value=healthy_host):
        result = distribution.distribute_fleet()
    assert result["exit_code"] == 0, result
    assert ("wheel", consumer_python, WHEEL_FIXTURE_VERSION) in installs
    assert ("editable", dev_python, str(tmp_path)) in installs
    assert not any(event[0] == "wheel" and event[1] == dev_python for event in installs)


def test_distribution_check_reports_install_kind_reasons(tmp_path):
    target = wheel_fixture_target(tmp_path)
    probe = {"confirmed": True, "version": WHEEL_FIXTURE_VERSION,
             "metadata_version": WHEEL_FIXTURE_VERSION, "sha": "wheel-sha-irrelevant",
             "dirty": False, "gate_enforcing": True}
    correct_python = str(tmp_path / "correct" / "bin" / "python")
    wrong_python = str(tmp_path / "wrong" / "bin" / "python")
    editable_python = str(tmp_path / "editable" / "bin" / "python")
    verified_at = datetime.now(timezone.utc).isoformat()
    persisted = {"schema_version": manifest.MANIFEST_SCHEMA_VERSION,
                 "writer_version": manifest.SPENDGUARD_WRITER_VERSION,
                 "last_verified_at": verified_at,
                 "enumeration_root_mtimes": manifest.manifest_scan_root_mtimes(()),
                 "gaps": [], "surfaces": [
        {"surface_kind": "venv", "canonical_path": correct_python,
         "install_kind": manifest.MANIFEST_WHEEL_KIND, "wheel_version": WHEEL_FIXTURE_VERSION,
         "last_verified_at": verified_at,
         "audit_cache_key": manifest.interpreter_audit_cache_key(correct_python)},
        {"surface_kind": "venv", "canonical_path": wrong_python,
         "install_kind": manifest.MANIFEST_WHEEL_KIND, "wheel_version": WHEEL_FIXTURE_OLD_VERSION,
         "last_verified_at": verified_at,
         "audit_cache_key": manifest.interpreter_audit_cache_key(wrong_python)},
        {"surface_kind": "venv", "canonical_path": editable_python,
         "install_kind": manifest.MANIFEST_EDITABLE_KIND, "wheel_version": None,
         "editable_origin": str(tmp_path / "other"), "last_verified_at": verified_at,
         "audit_cache_key": manifest.interpreter_audit_cache_key(editable_python)},
    ]}
    manifest_file = tmp_path / manifest.MANIFEST_FILENAME
    manifest_file.touch()
    healthy_host = distribution.distribution_surface_row("host", "host", True, True)
    with patch.object(distribution, "resolve_distribution_target", return_value=target), patch.object(
            distribution, "distribution_policy", return_value=WHEEL_FIXTURE_POLICY), patch.object(
            distribution.consumer_audit, "audit_consumer_workspaces",
            return_value={"consumers": [], "pruned_at_depth": []}), patch.object(
            manifest, "manifest_path", return_value=manifest_file), patch.object(
            manifest, "read_surface_manifest", return_value=persisted), patch.object(
            distribution, "probe_distribution_interpreter", return_value=probe), patch.object(
            distribution, "attest_distribution_api_consumers", return_value=[]), patch.object(
            distribution, "attest_distribution_daemons", return_value=[]), patch.object(
            distribution, "attest_distribution_host", return_value=healthy_host):
        result = distribution.distribute_fleet(check=True, roots=[])
    rows = {row["surface"]: row for row in result["surfaces"]}
    assert rows[correct_python]["straggler_reason"] is None
    assert rows[wrong_python]["straggler_reason"] == "wrong_version"
    assert rows[editable_python]["straggler_reason"] == "still_editable"
    assert result["exit_code"] != 0


if __name__ == "__main__":
    import tempfile
    with tempfile.TemporaryDirectory() as temporary_directory:
        test_distribution_wheel_build_cache_and_version_cross_check(Path(temporary_directory))
    with tempfile.TemporaryDirectory() as temporary_directory:
        test_distribution_consumer_uses_wheel_and_canonical_dev_stays_editable(Path(temporary_directory))
    with tempfile.TemporaryDirectory() as temporary_directory:
        test_distribution_check_reports_install_kind_reasons(Path(temporary_directory))
    print("PASS distribution wheel convergence")
