"""Prove the persisted manifest is complete and records real install kinds."""
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from unittest.mock import patch

os.environ.setdefault("SPENDGUARD_NO_AUTOINSTALL", "1")
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from spendguard import manifest  # noqa: E402

MANIFEST_FIXTURE_TIME = datetime(2026, 1, 2, tzinfo=timezone.utc)
MANIFEST_FIXTURE_WHEEL_VERSION = "fixture-wheel-version"
MANIFEST_FIXTURE_DAEMON_PID = 101
MANIFEST_FIXTURE_WORKER_PID = 202
MANIFEST_FIXTURE_UNREPRESENTABLE_PID = 303
MANIFEST_FIXTURE_WORKER_ROLE = "worker"
MANIFEST_FIXTURE_DISTRIBUTION_METADATA = "Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n"
MANIFEST_FIXTURE_WHEEL_DIRECT_URL = {"url": "file:///fixture/llm_spendguard.whl", "archive_info": {}}
MANIFEST_FIXTURE_EDITABLE_DIRECT_URL = {"url": "file:///fixture/source", "dir_info": {"editable": True}}


def run_manifest_probe_fixture(root, direct_url):
    normalized_distribution_name = manifest.MANIFEST_DISTRIBUTION_NAME.replace("-", "_")
    distribution_metadata = root / f"{normalized_distribution_name}-fixture.dist-info"
    distribution_metadata.mkdir()
    (distribution_metadata / "METADATA").write_text(
        MANIFEST_FIXTURE_DISTRIBUTION_METADATA.format(
            name=manifest.MANIFEST_DISTRIBUTION_NAME, version=MANIFEST_FIXTURE_WHEEL_VERSION))
    (distribution_metadata / "direct_url.json").write_text(json.dumps(direct_url))
    unrelated_checkout = root / "unrelated-checkout"
    (unrelated_checkout / manifest.MANIFEST_IMPORT_PACKAGE_NAME).mkdir(parents=True)
    (root / "unrelated-spendguard.pth").write_text(str(unrelated_checkout))
    environment = {**os.environ, "PYTHONPATH": os.pathsep.join((str(root), os.environ.get("PYTHONPATH", "")))}
    completed = subprocess.run(
        [sys.executable, "-c", manifest.MANIFEST_INTERPRETER_PROBE,
         manifest.MANIFEST_DISTRIBUTION_NAME, manifest.MANIFEST_IMPORT_PACKAGE_NAME],
        capture_output=True, text=True, check=True, env=environment,
    )
    return json.loads(completed.stdout)


def test_manifest_probe_ignores_unrelated_spendguard_pth_for_wheel_install():
    with tempfile.TemporaryDirectory() as temporary:
        result = run_manifest_probe_fixture(Path(temporary), MANIFEST_FIXTURE_WHEEL_DIRECT_URL)
    assert result == {"install_kind": manifest.MANIFEST_WHEEL_KIND,
                      "wheel_version": MANIFEST_FIXTURE_WHEEL_VERSION, "editable_origin": None}


def test_manifest_probe_recognizes_own_pep660_editable_metadata():
    with tempfile.TemporaryDirectory() as temporary:
        result = run_manifest_probe_fixture(Path(temporary), MANIFEST_FIXTURE_EDITABLE_DIRECT_URL)
    assert result == {"install_kind": manifest.MANIFEST_EDITABLE_KIND, "wheel_version": None,
                      "editable_origin": "/fixture/source"}


def test_surface_manifest_persists_complete_discovery_with_editable_and_wheel_kinds():
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        editable_python = root / "editable" / "bin" / "python"
        wheel_python = root / "wheel" / "bin" / "python"
        for executable in (editable_python, wheel_python):
            executable.parent.mkdir(parents=True)
            executable.touch()
        discovery = {"interpreters": [
            {"python": str(editable_python), "venv": str(editable_python.parent.parent), "kind": "venv"},
            {"python": str(wheel_python), "venv": str(wheel_python.parent.parent), "kind": "venv"},
        ], "gaps": []}

        def fixture_install_kind(python_executable, *, timeout_seconds):
            assert timeout_seconds > 0
            if python_executable == str(editable_python.resolve()):
                return {"install_kind": manifest.MANIFEST_EDITABLE_KIND, "wheel_version": None}
            if python_executable == str(wheel_python.resolve()):
                return {"install_kind": manifest.MANIFEST_WHEEL_KIND,
                        "wheel_version": MANIFEST_FIXTURE_WHEEL_VERSION}
            raise AssertionError(f"unexpected interpreter: {python_executable}")

        with patch.object(manifest.config, "HOME", root / "state"), \
             patch.object(manifest.setup, "discover_distribution_interpreters", return_value=discovery), \
             patch.object(manifest, "inspect_interpreter_install_kind", side_effect=fixture_install_kind), \
             patch.object(manifest, "_manifest_served_identity",
                          side_effect=lambda python, settings: {"served_sha": Path(python).parent.parent.name,
                                                                "role": "interpreter"}), \
             patch.object(manifest.fleet, "running_consumers", return_value=[]), \
             patch.object(manifest.shutil, "which", return_value=None):
            result = manifest.build_surface_manifest(settings={}, now=MANIFEST_FIXTURE_TIME)
            persisted = manifest.read_surface_manifest()

        expected_paths = {str(editable_python.resolve()), str(wheel_python.resolve())}
        venv_rows = {row["canonical_path"]: row for row in result["surfaces"]
                     if row["surface_kind"] == "venv"}
        assert set(venv_rows) == expected_paths
        assert venv_rows[str(editable_python.resolve())]["install_kind"] == manifest.MANIFEST_EDITABLE_KIND
        assert venv_rows[str(wheel_python.resolve())]["install_kind"] == manifest.MANIFEST_WHEEL_KIND
        assert venv_rows[str(wheel_python.resolve())]["wheel_version"] == MANIFEST_FIXTURE_WHEEL_VERSION
        assert persisted == result
        assert json.loads((root / "state" / manifest.MANIFEST_FILENAME).read_text()) == result


def test_surface_manifest_accounts_for_daemon_and_non_daemon_running_consumers():
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        daemon_python = root / "daemon" / "bin" / "python"
        worker_python = root / "worker" / "bin" / "python"
        running_consumers = [
            {"pid": MANIFEST_FIXTURE_DAEMON_PID, "role": manifest.fleet.CONSUMER_ROLE_DAEMON,
             "python_executable": str(daemon_python), "supervision_ref": "fixture-daemon",
             "served_sha": "daemon-sha"},
            {"pid": MANIFEST_FIXTURE_WORKER_PID, "role": MANIFEST_FIXTURE_WORKER_ROLE,
             "python_executable": str(worker_python), "supervision_ref": None,
             "served_sha": "worker-sha"},
            {"pid": MANIFEST_FIXTURE_UNREPRESENTABLE_PID,
             "role": manifest.fleet.CONSUMER_ROLE_EPHEMERAL,
             "python_executable": None, "supervision_ref": None, "served_sha": "ephemeral-sha"},
        ]
        discovery = {"interpreters": [], "gaps": []}

        with patch.object(manifest.config, "HOME", root / "state"), \
             patch.object(manifest.setup, "discover_distribution_interpreters", return_value=discovery), \
             patch.object(manifest.fleet, "running_consumers", return_value=running_consumers), \
             patch.object(manifest.shutil, "which", return_value=None):
            result = manifest.build_surface_manifest(settings={}, now=MANIFEST_FIXTURE_TIME)

        accounted_roles = {row["role"] for row in result["surfaces"]}
        assert accounted_roles == {manifest.fleet.CONSUMER_ROLE_DAEMON, MANIFEST_FIXTURE_WORKER_ROLE}
        worker_surface = next(row for row in result["surfaces"]
                              if row["role"] == MANIFEST_FIXTURE_WORKER_ROLE)
        assert worker_surface["surface_kind"] == manifest.MANIFEST_RUNNING_CONSUMER_SURFACE_KIND
        assert worker_surface["canonical_path"] == str(worker_python.resolve())
        assert result["gaps"] == [{
            "pid": MANIFEST_FIXTURE_UNREPRESENTABLE_PID,
            "role": manifest.fleet.CONSUMER_ROLE_EPHEMERAL,
            "reason": manifest.MANIFEST_UNREPRESENTABLE_CONSUMER_REASON,
        }]


if __name__ == "__main__":
    test_manifest_probe_ignores_unrelated_spendguard_pth_for_wheel_install()
    test_manifest_probe_recognizes_own_pep660_editable_metadata()
    test_surface_manifest_persists_complete_discovery_with_editable_and_wheel_kinds()
    test_surface_manifest_accounts_for_daemon_and_non_daemon_running_consumers()
    print("PASS complete persisted surface manifest records editable and wheel installs")
