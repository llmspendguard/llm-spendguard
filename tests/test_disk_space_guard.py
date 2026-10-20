"""Disk admission must refuse low space, including through snapshot's broad catch.

Mutation control: replacing the free < floor comparison with False MUST fail the
below-floor assertion. All disk probes are mocked; no real disk filling or LLM calls.
"""
import json
import os
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from _pytest_fixture_bridge import align_test_startup_home

align_test_startup_home(prefix=__name__)

from spendguard import budget, config, disk_guard
from spendguard.gate import SpendGateRefused

TEST_FLOOR_BYTES = 4096
TEST_SHORTFALL_BYTES = 1
TEST_HIGHER_FLOOR_BYTES = TEST_FLOOR_BYTES * 2
TEST_CONFIG_FILENAME = "config.json"
TEST_NEW_DESTINATION = "pending/ledger.db"
TEST_SOURCE_FILENAME = "source.db"


def expect_disk_refusal(operation, *args, fragments=(), **kwargs):
    try:
        operation(*args, **kwargs)
    except SpendGateRefused as exc:
        for fragment in fragments:
            assert fragment in str(exc), (fragment, str(exc))
    else:
        raise AssertionError("unsafe disk write passed without SpendGateRefused")


with tempfile.TemporaryDirectory() as home:
    root = Path(home)
    destination = root / TEST_NEW_DESTINATION
    cfg_path = root / TEST_CONFIG_FILENAME
    environment = dict(os.environ)
    environment.pop(config.DISK_GUARD_FLOOR_ENV, None)
    with patch.dict(os.environ, environment, clear=True), patch.object(config, "CONFIG_JSON", cfg_path):
        cfg_path.write_text(json.dumps({"disk_guard": {"floor_bytes": TEST_FLOOR_BYTES}}))
        config.cfg_invalidate()
        free_bytes = TEST_FLOOR_BYTES - TEST_SHORTFALL_BYTES
        with patch.object(disk_guard.shutil, "disk_usage", return_value=SimpleNamespace(free=free_bytes)) as probe:
            expect_disk_refusal(disk_guard.require_disk_space, destination, fragments=(
                str(destination), f"free {free_bytes} bytes", f"floor {TEST_FLOOR_BYTES} bytes",
                f"shortfall {TEST_SHORTFALL_BYTES} bytes"))
            probe.assert_called_once_with(root)
        with patch.object(disk_guard.shutil, "disk_usage", return_value=SimpleNamespace(free=TEST_FLOOR_BYTES)):
            disk_guard.require_disk_space(destination)  # equality passes
        with patch.object(disk_guard.shutil, "disk_usage", return_value=SimpleNamespace(free=TEST_FLOOR_BYTES + TEST_SHORTFALL_BYTES)):
            disk_guard.require_disk_space(destination)
            cfg_path.write_text(json.dumps({"disk_guard": {"floor_bytes": TEST_HIGHER_FLOOR_BYTES}}))
            config.cfg_invalidate()
            expect_disk_refusal(disk_guard.require_disk_space, destination,
                                fragments=(f"floor {TEST_HIGHER_FLOOR_BYTES} bytes",))
            with patch.dict(os.environ, {config.DISK_GUARD_FLOOR_ENV: str(TEST_FLOOR_BYTES)}):
                disk_guard.require_disk_space(destination)
        with patch.dict(os.environ, {config.DISK_GUARD_FLOOR_ENV: "invalid"}):
            expect_disk_refusal(disk_guard.require_disk_space, destination, fragments=("cannot establish",))
        with patch.object(disk_guard.shutil, "disk_usage", side_effect=OSError("probe unavailable")):
            expect_disk_refusal(disk_guard.require_disk_space, destination, fragments=(str(destination), "probe unavailable"))
        with patch.object(disk_guard.shutil, "disk_usage", return_value=SimpleNamespace(free=free_bytes)):
            source = Mock()
            expect_disk_refusal(budget.snapshot, compact_source=source, compact_target=destination)
            source.execute.assert_not_called()
            with patch.object(config, "HOME", root), patch.object(config, "db_path", return_value=root / TEST_SOURCE_FILENAME):
                expect_disk_refusal(budget.snapshot, force=True)
            assert not list(root.rglob("*.db")), "refusal must precede DB creation"
        config.cfg_invalidate()
print("ok — disk floor refusal, config boundary, probe failure and snapshot propagation")
