"""Distribution discovery resolves venv aliases without inventing consumers."""
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("SPENDGUARD_TEST_ISOLATED", "1")
from spendguard import consumer_audit, fleet, setup


def create_distribution_fixture_venv(venv_directory):
    executable = venv_directory.joinpath(*consumer_audit.CONSUMER_AUDIT_POSIX_EXECUTABLE_PATHS[0])
    executable.parent.mkdir(parents=True)
    (venv_directory / consumer_audit.CONSUMER_AUDIT_VENV_CONFIGURATION_NAME).touch()
    executable.symlink_to(sys.executable)
    return executable


def test_distribution_enumerates_relative_and_absolute_symlinked_venvs_once():
    with tempfile.TemporaryDirectory() as temporary_directory:
        fixture_root = Path(temporary_directory)
        relative_repository = fixture_root / "relative-repository"
        relative_repository.mkdir()
        relative_venv = relative_repository / ".venv.nosync"
        create_distribution_fixture_venv(relative_venv)
        (relative_repository / ".venv").symlink_to(relative_venv.name, target_is_directory=True)

        absolute_repository = fixture_root / "absolute-repository"
        absolute_repository.mkdir()
        absolute_venv = fixture_root / "external-venv"
        create_distribution_fixture_venv(absolute_venv)
        (absolute_repository / ".venv").symlink_to(absolute_venv, target_is_directory=True)

        real_data_directory = fixture_root / "real-data"
        real_data_directory.mkdir()
        data_alias = relative_repository / "data"
        data_alias.symlink_to(real_data_directory, target_is_directory=True)
        cycle_alias = relative_repository / "cycle"
        cycle_alias.symlink_to(cycle_alias, target_is_directory=True)

        configured_settings = {
            "workspace_roots": (relative_repository, absolute_repository),
            "workspace_extra_venv_dirs": (),
            "consumer_audit_exempt": (),
        }
        with patch.object(fleet, "registered_interpreters", return_value=[]), patch.object(
                fleet, "discover_host_spendguard_installations", return_value=[]):
            discovery = setup.discover_distribution_interpreters(settings=configured_settings)

        discovered_venvs = [Path(row["venv"]).resolve() for row in discovery["interpreters"]
                            if row["kind"] == "venv"]
        assert discovered_venvs.count(relative_venv.resolve()) == 1, discovery
        assert discovered_venvs.count(absolute_venv.resolve()) == 1, discovery
        gap_surfaces = {Path(gap["surface"]) for gap in discovery["gaps"]}
        assert data_alias not in gap_surfaces, discovery
        assert cycle_alias not in gap_surfaces, discovery


test_distribution_enumerates_relative_and_absolute_symlinked_venvs_once()
