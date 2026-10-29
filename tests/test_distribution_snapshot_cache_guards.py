"""Guards for bounded, cached, isolated distribution observation."""
import atexit
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
from unittest.mock import patch
from unittest import TestCase

DISTRIBUTION_SNAPSHOT_GUARD_TEMP_PREFIX = "sg-distribution-snapshot-guards-"
DISTRIBUTION_MALFORMED_OWNER_MARKER = "malformed-owner-marker"
DISTRIBUTION_STALE_DIST_INFO_NAME = "llm_spendguard-stale.dist-info"
DISTRIBUTION_SNAPSHOT_FIFO_NAME = "discovered-unit.fifo"
DISTRIBUTION_SNAPSHOT_REGULAR_CONTROL_NAME = "regular-control.json"
DISTRIBUTION_SNAPSHOT_REGULAR_CONTROL_CONTENT = "unchanged regular evidence"
if not os.environ.get("SPENDGUARD_TEST_ISOLATED"):
    os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"
    os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(
        prefix=DISTRIBUTION_SNAPSHOT_GUARD_TEMP_PREFIX
    )
    os.execv(sys.executable, [sys.executable] + sys.argv)
os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp()
atexit.register(shutil.rmtree, os.environ["SPENDGUARD_HOME"], ignore_errors=True)
os.environ.setdefault("SPENDGUARD_NO_AUTOINSTALL", "1")
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from spendguard import adapters, config_schema, distribution, manifest


def seed_distribution_snapshot_guard_home(root):
    home = root / "state"
    home.mkdir()
    (home / "policy.json").write_text("policy")
    (home / "snapshots").mkdir()
    (home / "snapshots" / distribution.DISTRIBUTION_EPHEMERAL_MARKER).touch()
    (home / "snapshots" / "large.db").write_text("recovery")
    (home / "reclaim-backups").mkdir()
    (home / "reclaim-backups" / distribution.DISTRIBUTION_EPHEMERAL_MARKER).touch()
    (home / "reclaim-backups" / "large.db").write_text("backup")
    (home / "embed_fixture.jsonl").write_text("checkpoint")
    (home / "old.json~").write_text("rotation")
    return home


def guard_1_unchanged_snapshot_skips_bulk_copy_and_policy_touch_rebuilds(tmp_path):
    home = seed_distribution_snapshot_guard_home(tmp_path)
    real_copytree = distribution.shutil.copytree
    base_copies = []

    def count_base_copy(source, destination, *args, **kwargs):
        base_copies.append((Path(source), Path(destination)))
        return real_copytree(source, destination, *args, **kwargs)

    with patch.object(distribution.config, "HOME", home), \
         patch.object(distribution.shutil, "copytree", side_effect=count_base_copy):
        distribution.ensure_distribution_probe_base()
        distribution.ensure_distribution_probe_base()
        assert len(base_copies) == 1
        policy = home / "policy.json"
        policy.write_text("changed-policy")
        distribution.ensure_distribution_probe_base()
        assert len(base_copies) == 2


def guard_2_excludes_outputs_but_keeps_policy_and_consistent_ledger(tmp_path):
    home = seed_distribution_snapshot_guard_home(tmp_path)
    ledger = tmp_path / "configured.db"
    ledger.write_bytes(b"ledger-evidence")
    (home / distribution.config.CONFIG_JSON.name).write_text(json.dumps(
        {"budget": {"db_path": str(ledger)}}))
    with patch.object(distribution.config, "HOME", home), \
         patch.object(distribution.config, "CONFIG_JSON", home / distribution.config.CONFIG_JSON.name), \
         patch.object(distribution.fleet, "sqlite_snapshot_source_evidence",
                      side_effect=lambda path: ((Path(path).stat().st_size, Path(path).stat().st_mtime_ns),
                                                Path(path).read_bytes()) if Path(path).is_file() else None):
        distribution.config.cfg_invalidate()
        with distribution.distribution_probe_environment() as environment:
            snapshot = Path(environment["SPENDGUARD_HOME"])
            assert (snapshot / "policy.json").read_text() == "policy"
            assert not (snapshot / "snapshots").exists()
            assert not (snapshot / "reclaim-backups").exists()
            assert (snapshot / "embed_fixture.jsonl").read_text() == "checkpoint"
            policy = json.loads((snapshot / distribution.config.CONFIG_JSON.name).read_text())
            assert Path(policy["budget"]["db_path"]).read_bytes() == b"ledger-evidence"


def guard_3_probe_cleanup_on_success_exception_and_stale_sweep(tmp_path):
    home = seed_distribution_snapshot_guard_home(tmp_path)
    prefix = distribution.distribution_config_value("probe_temp_prefix")
    marker = distribution.distribution_config_value("probe_temp_marker_name")
    dead_owner = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(0.1)"])
    dead_owner_create_time = repr(distribution.fleet.psutil.Process(dead_owner.pid).create_time())
    dead_owner.wait()
    stale = Path(tempfile.mkdtemp(prefix=prefix, dir=tmp_path))
    (stale / marker).write_text(json.dumps({
        distribution.DISTRIBUTION_PROBE_OWNER_PID_KEY: dead_owner.pid,
        distribution.DISTRIBUTION_PROBE_OWNER_CREATE_TIME_KEY: dead_owner_create_time,
    }))
    (stale / home.name).mkdir(exist_ok=True)
    live = Path(tempfile.mkdtemp(prefix=prefix, dir=tmp_path))
    distribution.write_distribution_probe_owner_marker(live / marker)
    malformed = Path(tempfile.mkdtemp(prefix=prefix, dir=tmp_path))
    (malformed / marker).write_text(DISTRIBUTION_MALFORMED_OWNER_MARKER)
    with patch.object(distribution.config, "HOME", home), \
         patch.object(distribution.tempfile, "gettempdir", return_value=str(tmp_path)):
        with TestCase().assertLogs(distribution.DISTRIBUTION_SNAPSHOT_LOGGER.name, level="WARNING") as captured:
            distribution.sweep_stale_distribution_probe_roots()
        assert not stale.exists()
        assert live.exists()
        assert malformed.exists()
        assert any(str(malformed) in message for message in captured.output)
        with distribution.distribution_probe_environment() as environment:
            successful_root = Path(environment["SPENDGUARD_HOME"]).parent
        assert not successful_root.exists()
        try:
            with distribution.distribution_probe_environment() as environment:
                failed_root = Path(environment["SPENDGUARD_HOME"]).parent
                raise RuntimeError("forced body failure")
        except RuntimeError:
            pass
        assert not failed_root.exists()
    distribution.shutil.rmtree(live)
    distribution.shutil.rmtree(malformed)


def build_distribution_snapshot_guard_manifest(home, *, writer_version, verified_at, roots):
    return {"schema_version": manifest.MANIFEST_SCHEMA_VERSION, "writer_version": writer_version,
            "last_verified_at": verified_at.isoformat(),
            "enumeration_root_mtimes": manifest.manifest_scan_root_mtimes(roots),
            "surfaces": [], "gaps": []}


def guard_4_check_manifest_version_refusal_and_output_metadata(tmp_path):
    now = datetime.now(timezone.utc)
    matching = build_distribution_snapshot_guard_manifest(
        tmp_path, writer_version=distribution.SPENDGUARD_WRITER_VERSION,
        verified_at=now, roots=[tmp_path]
    )
    stale = {**matching, "writer_version": "different-writer"}
    with patch.object(distribution.config, "_cfg_get", side_effect=lambda section, key, default: default):
        matching_status = distribution.distribution_manifest_cache_status(matching, [tmp_path], {}, now=now)
        stale_status = distribution.distribution_manifest_cache_status(stale, [tmp_path], {}, now=now)
    assert matching_status["authoritative"] is True
    assert stale_status["authoritative"] is False
    assert "stale, re-enumerate" in stale_status["reason"]
    rendered = distribution.render_distribution_table(
        {"manifest_cache": stale_status, "surfaces": [], "summary": {}, "exit_code": 1})
    assert "writer-version=different-writer" in rendered and f"timestamp={now.isoformat()}" in rendered


def guard_5_mutating_path_live_probes_before_cached_candidate_install(tmp_path):
    python = str(tmp_path / "venv" / "bin" / "python")
    Path(python).parent.mkdir(parents=True)
    Path(python).touch()
    events = []
    target = {"sha": "target-sha", "version": "target-version", "green_version": "target-version",
              "checkout": str(tmp_path)}
    cached_install = {"install_kind": manifest.MANIFEST_WHEEL_KIND,
                      "wheel_version": target["version"], "editable_origin": None}
    inventory = {"interpreters": [{"python": python, "venv": str(tmp_path / "venv"), "kind": "venv",
                                     "install": cached_install}], "gaps": []}
    stale_proof = {"confirmed": True, "version": "stale-version", "metadata_version": "stale-version",
                   "sha": None, "dirty": True, "gate_enforcing": True}
    target_proof = {"confirmed": True, "version": "target-version", "metadata_version": "target-version",
                    "sha": None, "dirty": True, "gate_enforcing": True}

    def probe_after_distribution_install(candidate, settings):
        events.append(("probe", candidate))
        return target_proof if any(event[0] == "install" for event in events) else stale_proof
    with patch.object(distribution, "resolve_distribution_target", return_value=target), \
         patch.object(distribution.config, "consumer_audit_settings", return_value={}), \
         patch.object(distribution.setup, "discover_distribution_interpreters", return_value=inventory), \
         patch.object(distribution.consumer_audit, "audit_consumer_workspaces",
                      return_value={"consumers": [], "pruned_at_depth": []}), \
         patch.object(distribution, "prepare_distribution_checkout", return_value=str(tmp_path)), \
         patch.object(distribution, "ensure_distribution_wheel"), \
         patch.object(distribution, "probe_distribution_interpreter",
                      side_effect=probe_after_distribution_install), \
         patch.object(distribution.setup, "refresh_consumer_wheel_package",
                      side_effect=lambda candidate, wheel, version, timeout: events.append(("install", candidate))), \
         patch.object(distribution.setup, "install_hook", return_value=0), \
         patch.object(distribution, "inspect_distribution_install", return_value=cached_install), \
         patch.object(distribution, "attest_distribution_api_consumers", return_value=[]), \
         patch.object(distribution, "attest_distribution_daemons", return_value=[]), \
         patch.object(distribution, "attest_distribution_host",
                      return_value=distribution.distribution_surface_row("host", "host", True, True)):
        distribution.distribute_fleet(check=False, dry_run=False)
    assert events[:2] == [("probe", python), ("install", python)]


def guard_6_enumeration_ttl_root_mtime_and_audit_key_invalidation(tmp_path):
    now = datetime.now(timezone.utc)
    persisted = build_distribution_snapshot_guard_manifest(
        tmp_path, writer_version=distribution.SPENDGUARD_WRITER_VERSION,
        verified_at=now, roots=[tmp_path]
    )
    surface = {"canonical_path": str(tmp_path / "venv" / "bin" / "python"),
               "last_verified_at": now.isoformat()}
    Path(surface["canonical_path"]).parent.mkdir(parents=True)
    Path(surface["canonical_path"]).touch()
    dist_info = tmp_path / "venv" / "lib" / "python-fixture" / "site-packages" / "llm_spendguard-fixture.dist-info"
    dist_info.mkdir(parents=True)
    persisted["enumeration_root_mtimes"] = manifest.manifest_scan_root_mtimes([tmp_path])
    surface["audit_cache_key"] = manifest.interpreter_audit_cache_key(surface["canonical_path"])
    with patch.object(distribution.config, "_cfg_get", side_effect=lambda section, key, default: default):
        assert distribution.distribution_manifest_cache_status(persisted, [tmp_path], {}, now=now)["authoritative"]
        expired = distribution.distribution_manifest_cache_status(
            persisted, [tmp_path], {}, now=now + timedelta(days=2))
        assert not expired["authoritative"]
        assert distribution.distribution_manifest_install_cache_valid(surface, now=now)
        os.utime(dist_info, ns=(dist_info.stat().st_atime_ns, dist_info.stat().st_mtime_ns + 1))
        assert not distribution.distribution_manifest_install_cache_valid(surface, now=now)
        with patch.object(manifest, "SPENDGUARD_WRITER_VERSION", "next-writer"):
            assert not distribution.distribution_manifest_install_cache_valid(surface, now=now)
    shared_mtime = dist_info.stat().st_mtime_ns
    stale_dist_info = dist_info.parent / DISTRIBUTION_STALE_DIST_INFO_NAME
    stale_dist_info.mkdir()
    os.utime(stale_dist_info, ns=(shared_mtime, shared_mtime))
    os.utime(dist_info, ns=(shared_mtime, shared_mtime))
    key_with_stale_candidate = manifest.interpreter_audit_cache_key(surface["canonical_path"])
    stale_dist_info.rmdir()
    key_without_stale_candidate = manifest.interpreter_audit_cache_key(surface["canonical_path"])
    assert key_with_stale_candidate != key_without_stale_candidate
    stale_dist_info.mkdir()
    real_path_stat = Path.stat

    def fail_stale_distribution_metadata_stat(path, *args, **kwargs):
        if path.name == DISTRIBUTION_STALE_DIST_INFO_NAME:
            raise OSError(manifest.MANIFEST_DIST_INFO_UNSTATTABLE_SENTINEL)
        return real_path_stat(path, *args, **kwargs)

    with patch.object(Path, "stat", fail_stale_distribution_metadata_stat):
        key_with_unstattable_candidate = manifest.interpreter_audit_cache_key(surface["canonical_path"])
    assert key_with_unstattable_candidate != key_without_stale_candidate
    assert any(candidate[1] == manifest.MANIFEST_DIST_INFO_UNSTATTABLE_SENTINEL
               for candidate in key_with_unstattable_candidate["dist_info_candidates"])
    os.utime(tmp_path, ns=(tmp_path.stat().st_atime_ns, tmp_path.stat().st_mtime_ns + 1))
    with patch.object(distribution.config, "_cfg_get", side_effect=lambda section, key, default: default):
        assert not distribution.distribution_manifest_cache_status(persisted, [tmp_path], {}, now=now)["authoritative"]


def test_guard_7_new_operational_values_are_schema_defaults():
    expected = {"probe_cache_dirname", "probe_cache_root", "probe_cache_base_name",
                "probe_cache_record_name", "probe_temp_prefix", "probe_temp_marker_name",
                "enumeration_cache_ttl_s", "audit_cache_ttl_s"}
    entries = {entry["key"]: entry for entry in config_schema.SETTINGS
               if entry["section"] == "distribute" and entry["key"] in expected}
    assert set(entries) == expected
    with patch.object(distribution.config, "_cfg_get", side_effect=lambda section, key, default: default):
        assert {key: distribution.distribution_config_value(key) for key in expected} == {
            key: entry["default"] for key, entry in entries.items()}


def guard_8_vanished_walked_entry_differs_from_never_present(tmp_path):
    home = tmp_path / "state"
    home.mkdir()
    vanished_entry = home / "vanished.json"
    vanished_entry.write_text("walked evidence")
    real_path_stat = Path.stat

    def raise_for_vanished_distribution_snapshot_entry(path, *args, **kwargs):
        if path == vanished_entry:
            raise FileNotFoundError(path)
        return real_path_stat(path, *args, **kwargs)

    with patch.object(Path, "stat", raise_for_vanished_distribution_snapshot_entry):
        vanished_fingerprint = distribution.distribution_snapshot_fingerprint(home)
    vanished_entry.unlink()
    absent_fingerprint = distribution.distribution_snapshot_fingerprint(home)
    assert vanished_fingerprint != absent_fingerprint


def guard_9_nonregular_entry_presence_changes_fingerprint(tmp_path):
    if not hasattr(os, "mkfifo"):
        print("  [SKIP] os.mkfifo unavailable on this platform")
        return
    home = tmp_path / "state"
    home.mkdir()
    regular_control = home / DISTRIBUTION_SNAPSHOT_REGULAR_CONTROL_NAME
    regular_control.write_text(DISTRIBUTION_SNAPSHOT_REGULAR_CONTROL_CONTENT)
    fifo = home / DISTRIBUTION_SNAPSHOT_FIFO_NAME
    os.mkfifo(fifo)
    fingerprint_with_fifo = distribution.distribution_snapshot_fingerprint(home)
    regular_control_details = regular_control.stat()
    fifo.unlink()
    fingerprint_without_fifo = distribution.distribution_snapshot_fingerprint(home)
    assert fingerprint_with_fifo != fingerprint_without_fifo
    assert regular_control.stat() == regular_control_details


def guard_10_unknown_named_entries_are_fingerprinted_and_copied(tmp_path):
    home = tmp_path / "state"
    home.mkdir()
    user_snapshots = home / "snapshots"
    user_snapshots.mkdir()
    required_policy = user_snapshots / "required-policy.json"
    required_policy.write_text("required")
    user_backup = home / "policy.bak"
    user_backup.write_text("user backup")
    encoded_manifests = []
    real_json_dumps = distribution.json.dumps

    def capture_distribution_snapshot_manifest(value, *args, **kwargs):
        if isinstance(value, list):
            encoded_manifests.append(value)
        return real_json_dumps(value, *args, **kwargs)

    cache = tmp_path / "probe-cache"
    with patch.object(distribution.config, "HOME", home), \
         patch.object(distribution.json, "dumps", side_effect=capture_distribution_snapshot_manifest), \
         patch.object(distribution, "distribution_probe_cache_paths",
                      return_value=(cache, cache / "base", cache / "fingerprint.json")):
        distribution.distribution_snapshot_fingerprint(home)
        snapshot = distribution.rebuild_or_reuse_distribution_probe_base()
    fingerprint_paths = {row[0] for row in encoded_manifests[0]}
    assert required_policy.relative_to(home).as_posix() in fingerprint_paths
    assert user_backup.relative_to(home).as_posix() in fingerprint_paths
    assert (snapshot / required_policy.relative_to(home)).read_text() == "required"
    assert (snapshot / user_backup.relative_to(home)).read_text() == "user backup"


def guard_11_owned_output_and_configured_ledger_follow_provenance(tmp_path):
    home = tmp_path / "state"
    home.mkdir()
    owned_output = distribution.mark_distribution_ephemeral_dir(home / "snapshots")
    (owned_output / "large-output.db").write_text("output")
    ledger = home / "configured.db"
    ledger.write_bytes(b"ledger-evidence")
    (home / distribution.config.CONFIG_JSON.name).write_text(json.dumps(
        {"budget": {"db_path": str(ledger)}}))
    cache = tmp_path / "probe-cache"
    with patch.object(distribution.config, "HOME", home), \
         patch.object(distribution.config, "CONFIG_JSON", home / distribution.config.CONFIG_JSON.name), \
         patch.object(distribution, "distribution_probe_cache_paths",
                      return_value=(cache, cache / "base", cache / "fingerprint.json")), \
         patch.object(distribution.fleet, "sqlite_snapshot_source_evidence",
                      side_effect=lambda path: ((Path(path).stat().st_size, Path(path).stat().st_mtime_ns),
                                                Path(path).read_bytes()) if Path(path).is_file() else None):
        distribution.config.cfg_invalidate()
        snapshot = distribution.rebuild_or_reuse_distribution_probe_base()
        assert not (snapshot / owned_output.name).exists()
        assert not (snapshot / ledger.name).exists()
        distribution.distribution_snapshot_configured_ledgers(snapshot)
    cloned_ledger = snapshot / "db_path" / ledger.name
    assert cloned_ledger.read_bytes() == b"ledger-evidence"
    assert [path for path in snapshot.rglob(ledger.name)] == [cloned_ledger]


def guard_12_embed_checkpoints_follow_ephemeral_dir_provenance(tmp_path):
    home = seed_distribution_snapshot_guard_home(tmp_path)
    cache = tmp_path / "probe-cache"
    with patch.object(adapters.config, "HOME", home), \
         patch.object(distribution, "distribution_probe_cache_paths",
                      return_value=(cache, cache / "base", cache / "fingerprint.json")):
        checkpoint_dir = adapters._embed_checkpoint_dir()
        checkpoint = checkpoint_dir / "embed_model_hash.jsonl"
        checkpoint.write_text("checkpoint")
        assert checkpoint.parent == home / adapters.EMBED_CHECKPOINT_DIRNAME
        assert (checkpoint_dir / distribution.DISTRIBUTION_EPHEMERAL_MARKER).is_file()
        assert distribution.distribution_snapshot_excluded(home, adapters.EMBED_CHECKPOINT_DIRNAME)
        assert not distribution.distribution_snapshot_excluded(home, "embed_fixture.jsonl")
        snapshot = distribution.ensure_distribution_probe_base()
        assert not (snapshot / adapters.EMBED_CHECKPOINT_DIRNAME).exists()
        assert (snapshot / "embed_fixture.jsonl").read_text() == "checkpoint"


def guard_13_cold_legacy_embed_checkpoints_move_but_warm_ones_remain(tmp_path):
    home = seed_distribution_snapshot_guard_home(tmp_path)
    cache = tmp_path / "probe-cache"
    cold = home / "embed_cold_hash.jsonl"
    warm = home / "embed_warm_hash.jsonl"
    cold.write_text("cold-checkpoint")
    warm.write_text("warm-checkpoint")
    grace_seconds = adapters.EMBED_CHECKPOINT_INFLIGHT_GRACE_SECONDS
    now = time.time()
    cold_mtime = now - grace_seconds - grace_seconds
    os.utime(cold, (cold_mtime, cold_mtime))
    os.utime(warm, (now, now))
    with patch.object(adapters.config, "HOME", home), \
         patch.object(distribution.config, "HOME", home), \
         patch.object(distribution, "distribution_probe_cache_paths",
                      return_value=(cache, cache / "base", cache / "fingerprint.json")):
        migration_result = adapters.migrate_cold_legacy_embed_checkpoints()
        assert migration_result["moved"] == 1
        assert migration_result["retained"] >= 1
        checkpoint_dir = home / adapters.EMBED_CHECKPOINT_DIRNAME
        assert not cold.exists()
        assert (checkpoint_dir / cold.name).read_text() == "cold-checkpoint"
        assert warm.read_text() == "warm-checkpoint"
        assert adapters._embed_checkpoint_path(warm.name) == warm
        snapshot = distribution.ensure_distribution_probe_base()
        assert not (snapshot / adapters.EMBED_CHECKPOINT_DIRNAME).exists()
        assert not (snapshot / cold.name).exists()
        assert (snapshot / warm.name).read_text() == "warm-checkpoint"


def run_distribution_snapshot_guard_with_temporary_path(guard):
    with tempfile.TemporaryDirectory() as temporary_directory:
        guard(Path(temporary_directory))


def test_guard_1_unchanged_snapshot_skips_bulk_copy_and_policy_touch_rebuilds():
    run_distribution_snapshot_guard_with_temporary_path(
        guard_1_unchanged_snapshot_skips_bulk_copy_and_policy_touch_rebuilds
    )


def test_guard_2_excludes_outputs_but_keeps_policy_and_consistent_ledger():
    run_distribution_snapshot_guard_with_temporary_path(
        guard_2_excludes_outputs_but_keeps_policy_and_consistent_ledger
    )


def test_guard_3_probe_cleanup_on_success_exception_and_stale_sweep():
    run_distribution_snapshot_guard_with_temporary_path(
        guard_3_probe_cleanup_on_success_exception_and_stale_sweep
    )


def test_guard_4_check_manifest_version_refusal_and_output_metadata():
    run_distribution_snapshot_guard_with_temporary_path(
        guard_4_check_manifest_version_refusal_and_output_metadata
    )


def test_guard_5_mutating_path_live_probes_before_cached_candidate_install():
    run_distribution_snapshot_guard_with_temporary_path(
        guard_5_mutating_path_live_probes_before_cached_candidate_install
    )


def test_guard_6_enumeration_ttl_root_mtime_and_audit_key_invalidation():
    run_distribution_snapshot_guard_with_temporary_path(
        guard_6_enumeration_ttl_root_mtime_and_audit_key_invalidation
    )


def test_guard_8_vanished_walked_entry_differs_from_never_present():
    run_distribution_snapshot_guard_with_temporary_path(
        guard_8_vanished_walked_entry_differs_from_never_present
    )


def test_guard_9_nonregular_entry_presence_changes_fingerprint():
    run_distribution_snapshot_guard_with_temporary_path(
        guard_9_nonregular_entry_presence_changes_fingerprint
    )


def test_guard_10_unknown_named_entries_are_fingerprinted_and_copied():
    run_distribution_snapshot_guard_with_temporary_path(
        guard_10_unknown_named_entries_are_fingerprinted_and_copied
    )


def test_guard_11_owned_output_and_configured_ledger_follow_provenance():
    run_distribution_snapshot_guard_with_temporary_path(
        guard_11_owned_output_and_configured_ledger_follow_provenance
    )


def test_guard_12_embed_checkpoints_follow_ephemeral_dir_provenance():
    run_distribution_snapshot_guard_with_temporary_path(
        guard_12_embed_checkpoints_follow_ephemeral_dir_provenance
    )


def test_guard_13_cold_legacy_embed_checkpoints_move_but_warm_ones_remain():
    run_distribution_snapshot_guard_with_temporary_path(
        guard_13_cold_legacy_embed_checkpoints_move_but_warm_ones_remain
    )


if __name__ == "__main__":
    test_guard_1_unchanged_snapshot_skips_bulk_copy_and_policy_touch_rebuilds()
    test_guard_2_excludes_outputs_but_keeps_policy_and_consistent_ledger()
    test_guard_3_probe_cleanup_on_success_exception_and_stale_sweep()
    test_guard_4_check_manifest_version_refusal_and_output_metadata()
    test_guard_5_mutating_path_live_probes_before_cached_candidate_install()
    test_guard_6_enumeration_ttl_root_mtime_and_audit_key_invalidation()
    test_guard_7_new_operational_values_are_schema_defaults()
    test_guard_8_vanished_walked_entry_differs_from_never_present()
    test_guard_9_nonregular_entry_presence_changes_fingerprint()
    test_guard_10_unknown_named_entries_are_fingerprinted_and_copied()
    test_guard_11_owned_output_and_configured_ledger_follow_provenance()
    test_guard_12_embed_checkpoints_follow_ephemeral_dir_provenance()
    test_guard_13_cold_legacy_embed_checkpoints_move_but_warm_ones_remain()
    print("PASS distribution snapshot/cache guards")
