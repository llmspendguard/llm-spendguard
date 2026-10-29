"""Offline regression guards for effective-ledger exclusion and abandoned builds."""
import contextlib
import json
import os
from pathlib import Path
import sys
import tempfile
from unittest.mock import patch

from _pytest_fixture_bridge import align_test_startup_home

align_test_startup_home(prefix=__name__)

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from spendguard import distribution

PROBE_DISK_LEAK_SIDECARS = ("-wal", "-shm", "-journal")
PROBE_DISK_LEAK_INITIAL_CONTENT = b"initial ledger"
PROBE_DISK_LEAK_CHANGED_CONTENT = b"changed ledger with additional transactions"
PROBE_DISK_LEAK_MTIME_INCREMENT_NS = 1_000_000_000
PROBE_DISK_LEAK_PID_OFFSET = 1
PROBE_DISK_LEAK_STALE_CREATE_TIME = "0.0"


@contextlib.contextmanager
def isolated_distribution_disk_leak_home():
    with tempfile.TemporaryDirectory() as directory:
        home = Path(directory)
        policy = home / distribution.config.CONFIG_JSON.name
        policy.write_text(json.dumps({"budget": {"backend": "sqlite"}}))
        with patch.object(distribution.config, "HOME", home), \
             patch.object(distribution.config, "CONFIG_JSON", policy), \
             patch.dict(os.environ, {"SPENDGUARD_HOME": str(home), "SPENDGUARD_TEST_ISOLATED": "1"}):
            distribution.config.cfg_invalidate()
            try:
                yield home
            finally:
                distribution.config.cfg_invalidate()


def test_distribution_default_ledgers_and_sidecars_excluded():
    with isolated_distribution_disk_leak_home() as home:
        for resolved in (distribution.config.db_path(), distribution.config.lane_queue_db_path()):
            ledger = Path(resolved)
            for path in (ledger, *(Path(str(ledger) + suffix) for suffix in PROBE_DISK_LEAK_SIDECARS)):
                path.write_bytes(PROBE_DISK_LEAK_INITIAL_CONTENT)
                assert distribution.distribution_snapshot_excluded(home, path.name), path


def test_distribution_ledger_mutations_preserve_snapshot_fingerprint():
    with isolated_distribution_disk_leak_home() as home:
        ledgers = [Path(distribution.config.db_path()), Path(distribution.config.lane_queue_db_path())]
        for ledger in ledgers:
            ledger.write_bytes(PROBE_DISK_LEAK_INITIAL_CONTENT)
        before = distribution.distribution_snapshot_fingerprint(home)
        for ledger in ledgers:
            details = ledger.stat()
            ledger.write_bytes(PROBE_DISK_LEAK_CHANGED_CONTENT)
            os.utime(ledger, ns=(details.st_atime_ns,
                                details.st_mtime_ns + PROBE_DISK_LEAK_MTIME_INCREMENT_NS))
        assert distribution.distribution_snapshot_fingerprint(home) == before


def test_distribution_abandoned_builds_swept_but_live_owners_preserved():
    with isolated_distribution_disk_leak_home() as home:
        cache = home.parent / (home.name + "-cache")
        base = cache / str(distribution.distribution_config_value("probe_cache_base_name"))
        record = cache / str(distribution.distribution_config_value("probe_cache_record_name"))
        marker = str(distribution.distribution_config_value("probe_temp_marker_name"))
        dead_pid = os.getpid() + PROBE_DISK_LEAK_PID_OFFSET
        stale = cache / f"{base.name}.building-{dead_pid}"
        live = cache / f"{base.name}.building-{os.getpid()}"
        try:
            stale.mkdir(parents=True)
            live.mkdir()
            distribution.write_distribution_probe_owner_marker(live / marker)
            (stale / marker).write_text(json.dumps({
                distribution.DISTRIBUTION_PROBE_OWNER_PID_KEY: dead_pid,
                distribution.DISTRIBUTION_PROBE_OWNER_CREATE_TIME_KEY: PROBE_DISK_LEAK_STALE_CREATE_TIME,
            }))
            with patch.object(distribution.fleet, "_classify_fleet_pid_identity",
                              return_value=distribution.fleet._FLEET_PID_GONE) as classify:
                distribution.sweep_stale_distribution_probe_base_builds(cache, base)
            assert not stale.exists()
            assert live.exists()
            classify.assert_called_once_with(dead_pid, PROBE_DISK_LEAK_STALE_CREATE_TIME)
            distribution.shutil.rmtree(live)
            real_copytree = distribution.shutil.copytree

            def verify_distribution_build_owner_before_copy(source, destination, **kwargs):
                owner = json.loads((Path(destination) / marker).read_text())
                assert owner[distribution.DISTRIBUTION_PROBE_OWNER_PID_KEY] == os.getpid()
                assert owner[distribution.DISTRIBUTION_PROBE_OWNER_CREATE_TIME_KEY]
                return real_copytree(source, destination, **kwargs)

            with patch.object(distribution, "distribution_probe_cache_paths", return_value=(cache, base, record)), \
                 patch.object(distribution.shutil, "copytree", side_effect=verify_distribution_build_owner_before_copy):
                distribution.ensure_distribution_probe_base()
            stale.mkdir()
            (stale / marker).write_text(json.dumps({
                distribution.DISTRIBUTION_PROBE_OWNER_PID_KEY: dead_pid,
                distribution.DISTRIBUTION_PROBE_OWNER_CREATE_TIME_KEY: PROBE_DISK_LEAK_STALE_CREATE_TIME,
            }))
            with patch.object(distribution, "distribution_probe_cache_paths", return_value=(cache, base, record)), \
                 patch.object(distribution.fleet, "_classify_fleet_pid_identity",
                              return_value=distribution.fleet._FLEET_PID_GONE):
                distribution.ensure_distribution_probe_base()
            assert not stale.exists()
        finally:
            distribution.shutil.rmtree(cache, ignore_errors=True)


if __name__ == "__main__":
    test_distribution_default_ledgers_and_sidecars_excluded()
    test_distribution_ledger_mutations_preserve_snapshot_fingerprint()
    test_distribution_abandoned_builds_swept_but_live_owners_preserved()
