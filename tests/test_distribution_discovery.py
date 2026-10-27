"""Offline completeness and containment proof for distribution interpreter discovery."""
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("SPENDGUARD_TEST_ISOLATED", "1")
from spendguard import fleet, setup


def test_distribution_discovery_keeps_nested_and_symlinked_consumers():
    with tempfile.TemporaryDirectory() as temporary_directory:
        root = Path(temporary_directory)
        configured = {"workspace_roots": (root,), "workspace_extra_venv_dirs": (),
                      "consumer_audit_exempt": (root / "exempt",), "workspace_scan_depth": 1}
        consumers = (root / "project" / "deep" / "env", root / "other" / "env")
        for venv in (*consumers, root / "exempt" / "env"):
            (venv / "bin").mkdir(parents=True)
            (venv / "pyvenv.cfg").touch()
            (venv / "bin" / "python").symlink_to(sys.executable)
        fixture_host_python = root / "host-python"
        fixture_host_python.touch()
        with patch.object(fleet, "registered_interpreters", return_value=[]), patch.object(
                fleet, "discover_host_spendguard_installations", return_value=[]), patch.object(
                sys, "executable", str(fixture_host_python)), patch.object(sys, "prefix", sys.base_prefix):
            result = setup.discover_distribution_interpreters(settings=configured)
        found = {row["venv"] for row in result["interpreters"] if row["kind"] == "venv"}
        assert found == {str(venv) for venv in consumers}, result
        assert not result["gaps"], result


def test_distribution_discovery_names_broken_consumers_and_containment_gaps():
    with tempfile.TemporaryDirectory() as temporary_directory:
        root = Path(temporary_directory)
        (root / "broken").mkdir()
        (root / "broken" / "pyvenv.cfg").touch()
        configured = {"workspace_roots": (root,), "workspace_extra_venv_dirs": (),
                      "consumer_audit_exempt": ()}
        with patch.object(fleet, "registered_interpreters", return_value=[]), patch.object(
                fleet, "discover_host_spendguard_installations", return_value=[]):
            result = setup.discover_distribution_interpreters(settings=configured)
            assert any(gap["surface"] == str(root / "broken") for gap in result["gaps"])
            configured["distribution_scan_directory_budget"] = 1
            result = setup.discover_distribution_interpreters(settings=configured)
            assert any("budget" in gap["reason"] for gap in result["gaps"]), result

            del configured["distribution_scan_directory_budget"]
            with patch.object(setup.config, "_cfg_get", return_value=1) as configured_budget:
                result = setup.discover_distribution_interpreters(settings=configured)
            configured_budget.assert_called_once_with(
                "distribute", "scan_directory_budget", setup._DISTRIBUTION_SCAN_DIRECTORY_BUDGET_DEFAULT)
            assert any("budget" in gap["reason"] for gap in result["gaps"]), result


test_distribution_discovery_keeps_nested_and_symlinked_consumers()
test_distribution_discovery_names_broken_consumers_and_containment_gaps()


def test_distribution_enumeration_assertion_rejects_omitted_consumers():
    original_discovery = setup.discover_distribution_interpreters

    def omit_fixture_venvs_from_discovery(*arguments, **keywords):
        result = original_discovery(*arguments, **keywords)
        result["interpreters"] = [row for row in result["interpreters"] if row["kind"] != "venv"]
        return result

    with patch.object(setup, "discover_distribution_interpreters", omit_fixture_venvs_from_discovery):
        try:
            test_distribution_discovery_keeps_nested_and_symlinked_consumers()
        except AssertionError:
            return
    raise AssertionError("completeness guard accepted omitted consumers")


test_distribution_enumeration_assertion_rejects_omitted_consumers()
