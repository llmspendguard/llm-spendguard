"""Proving guards for install-aware respawn, complete daemon wiring, and non-blocking restart."""
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from unittest.mock import patch

from spendguard import codex_daemon_broker, fleet, lane_queue, mcp_fd_lifecycle, mcp_server, release, serve
from spendguard import manifest

B2B_RUNNING_VERSION = "1.0.running"
B2B_INSTALLED_VERSION = "1.1.installed"
B2B_GREEN_SHA = "b2b-green"
B2B_OLD_SHA = "b2b-old"
B2B_LABEL = "com.example.b2b"
B2B_PID = 165
B2B_EPHEMERAL_PORT = 0
B2B_BOUNDED_DRAIN_ITERATIONS = 0
B2B_IMMEDIATE_HEALTH_INTERVAL = 0
B2B_RPC_METHOD = "fixture"
B2B_METADATA = "Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n"
B2B_UNRESOLVED_IMPORT_PACKAGE = f"{manifest.MANIFEST_IMPORT_PACKAGE_NAME}_fixture_missing"


def test_b2b_install_kind_aware_respawn_is_loop_safe():
    import spendguard
    with patch.object(spendguard, "__version__", B2B_RUNNING_VERSION), patch.object(
            manifest, "inspect_current_interpreter_install_kind",
            return_value={"install_kind": manifest.MANIFEST_WHEEL_KIND,
                          "wheel_version": B2B_INSTALLED_VERSION}):
        assert release.should_respawn() is True
    with patch.object(spendguard, "__version__", B2B_RUNNING_VERSION), patch.object(
            manifest, "inspect_current_interpreter_install_kind",
            return_value={"install_kind": manifest.MANIFEST_WHEEL_KIND,
                          "wheel_version": B2B_RUNNING_VERSION}):
        assert release.should_respawn() is False
    editable = {"install_kind": manifest.MANIFEST_EDITABLE_KIND, "wheel_version": None}
    with patch.object(manifest, "inspect_current_interpreter_install_kind", return_value=editable), \
         patch.object(release, "stale_vs_green", return_value=True), \
         patch.object(release, "green_pointer", return_value={"sha": B2B_GREEN_SHA}), \
         patch.object(release, "_git_src", return_value=B2B_GREEN_SHA):
        assert release.should_respawn() is True
    with patch.object(manifest, "inspect_current_interpreter_install_kind", return_value=editable), \
         patch.object(release, "stale_vs_green", return_value=True), \
         patch.object(release, "green_pointer", return_value={"sha": B2B_GREEN_SHA}), \
         patch.object(release, "_git_src", return_value=B2B_OLD_SHA):
        assert release.should_respawn() is False
    with patch.object(manifest, "inspect_current_interpreter_install_kind", return_value={
            "install_kind": manifest.MANIFEST_UNKNOWN_KIND, "wheel_version": None,
            "verification_error": "fixture unresolved"}):
        assert release.should_respawn() is False


def test_b2b_self_respawn_decision_never_uses_cross_interpreter_subprocess():
    manifest._cached_current_distribution_install_kind.cache_clear()
    with patch.object(manifest, "determine_distribution_install_kind", return_value={
            "install_kind": manifest.MANIFEST_WHEEL_KIND, "wheel_version": B2B_INSTALLED_VERSION,
            "editable_origin": None}), \
         patch.object(manifest.importlib.metadata, "version", return_value=B2B_INSTALLED_VERSION), \
         patch.object(manifest, "inspect_interpreter_install_kind",
                      side_effect=AssertionError("cross-interpreter probe used for self inspection")), \
         patch.object(manifest.subprocess, "run", side_effect=AssertionError("subprocess forked")):
        assert release.current_respawn_decision()["should_respawn"] is True
        assert release.should_respawn() is True
    manifest._cached_current_distribution_install_kind.cache_clear()


def test_b2b_install_kind_ssot_agrees_in_process_and_cross_interpreter():
    fixtures = (
        ({"url": "file:///fixture/source", "dir_info": {"editable": True}},
         manifest.MANIFEST_EDITABLE_KIND),
        ({"url": "file:///fixture/package.whl", "archive_info": {}}, manifest.MANIFEST_WHEEL_KIND),
        ({}, manifest.MANIFEST_UNKNOWN_KIND),
    )
    for direct_url, expected_kind in fixtures:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            normalized_name = manifest.MANIFEST_DISTRIBUTION_NAME.replace("-", "_")
            metadata_root = root / f"{normalized_name}-fixture.dist-info"
            metadata_root.mkdir()
            (metadata_root / "METADATA").write_text(B2B_METADATA.format(
                name=manifest.MANIFEST_DISTRIBUTION_NAME, version=B2B_INSTALLED_VERSION))
            (metadata_root / "direct_url.json").write_text(json.dumps(direct_url))
            environment = {"PYTHONPATH": str(root)}
            completed = subprocess.run(
                [sys.executable, "-c", manifest.MANIFEST_INTERPRETER_PROBE,
                 manifest.MANIFEST_DISTRIBUTION_NAME, B2B_UNRESOLVED_IMPORT_PACKAGE],
                capture_output=True, text=True, check=True, env=environment,
            )
            cross_interpreter = json.loads(completed.stdout)
            distribution = manifest.importlib.metadata.PathDistribution(metadata_root)
            with patch.object(manifest.importlib.metadata, "distribution", return_value=distribution), \
                 patch.object(manifest.importlib.util, "find_spec", return_value=None):
                in_process = manifest.determine_distribution_install_kind(
                    manifest.MANIFEST_DISTRIBUTION_NAME, B2B_UNRESOLVED_IMPORT_PACKAGE)
        assert in_process == cross_interpreter, (in_process, cross_interpreter)
        assert in_process["install_kind"] == expected_kind


def test_b2b_self_install_kind_cache_does_not_cache_wheel_version():
    manifest._cached_current_distribution_install_kind.cache_clear()
    versions = iter((B2B_RUNNING_VERSION, B2B_INSTALLED_VERSION))
    with patch.object(manifest, "determine_distribution_install_kind", return_value={
            "install_kind": manifest.MANIFEST_WHEEL_KIND, "wheel_version": "discarded",
            "editable_origin": None}) as determine_kind, \
         patch.object(manifest.importlib.metadata, "version", side_effect=lambda _name: next(versions)):
        first = manifest.inspect_current_interpreter_install_kind()
        second = manifest.inspect_current_interpreter_install_kind()
    manifest._cached_current_distribution_install_kind.cache_clear()
    assert determine_kind.call_count == 1
    assert first["wheel_version"] == B2B_RUNNING_VERSION
    assert second["wheel_version"] == B2B_INSTALLED_VERSION


def test_b2b_complete_supervised_daemon_inventory_has_respawn_safe_points():
    respawn_checks = []

    def record_respawn_check(*_args, **_kwargs):
        respawn_checks.append(None)
        return False

    stdio_request = json.dumps({"jsonrpc": "2.0", "id": B2B_PID, "method": B2B_RPC_METHOD})
    with patch.object(mcp_server, "_arm_and_verify_mcp_daemon_gate"), \
         patch.object(fleet, "register_consumer"), \
         patch.object(mcp_server, "_auto_respawn_enabled", return_value=True), \
         patch.object(mcp_server, "handle", return_value={"jsonrpc": "2.0", "id": B2B_PID}), \
         patch.object(release, "should_respawn", side_effect=record_respawn_check):
        mcp_server.serve_stdio(io.StringIO(stdio_request), io.StringIO())
    assert len(respawn_checks) == 1, "MCP stdio did not check for respawn after its service tick"

    class RecordingHTTPServerBase:
        def __init__(self, *_args, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def serve_forever(self, **_kwargs):
            self.service_actions()

    respawn_checks.clear()
    with patch("http.server.ThreadingHTTPServer", RecordingHTTPServerBase), \
         patch.object(mcp_server, "_auto_respawn_enabled", return_value=True), \
         patch.object(mcp_server, "_prewarm_codex_daemon"), \
         patch.object(fleet, "register_consumer"), \
         patch.object(mcp_fd_lifecycle, "mcp_descriptor_policy",
                      return_value=(None, None, None)), \
         patch.object(mcp_fd_lifecycle, "raise_mcp_descriptor_soft_limit"), \
         patch.object(mcp_fd_lifecycle, "enforce_mcp_descriptor_headroom"), \
         patch.object(mcp_fd_lifecycle, "service_mcp_spend_deadletters"), \
         patch.object(release, "exit_if_stale", side_effect=record_respawn_check):
        mcp_server.serve_http(port=B2B_EPHEMERAL_PORT)
    assert len(respawn_checks) == 1, "MCP HTTP service_actions did not check for respawn"

    class RecordingAskServer:
        def service_actions(self):
            pass

        def serve_forever(self):
            self.service_actions()

        def server_close(self):
            pass

    respawn_checks.clear()
    with patch("spendguard.require"), patch.object(fleet, "register_consumer"), \
         patch.object(serve, "make_server", return_value=RecordingAskServer()), \
         patch.object(release, "exit_if_stale", side_effect=record_respawn_check):
        serve.serve(port=B2B_EPHEMERAL_PORT)
    assert len(respawn_checks) == 1, "ask server service_actions did not check for respawn"

    broker = object.__new__(codex_daemon_broker._CodexDaemonBroker)
    broker._stopping = type("OneTickStop", (), {"wait": lambda self, _timeout: bool(respawn_checks)})()
    respawn_checks.clear()
    with patch.object(broker, "_become_owner_if_available", return_value=True), \
         patch.object(codex_daemon_broker.config, "codex_daemon_pool_settings",
                      return_value={"health_interval_s": B2B_IMMEDIATE_HEALTH_INTERVAL}), \
         patch.object(release, "exit_if_stale", side_effect=record_respawn_check):
        assert broker.broker_owner_serve_forever() is True
    assert len(respawn_checks) == 1, "codex broker owner loop did not check for respawn"

    def drive_bounded_drain(forever):
        respawn_checks.clear()
        with patch.object(lane_queue, "queue_depth", return_value={
                "pending": 0, "leased": 0, "queued_batch": 0}), \
             patch.object(lane_queue, "optimize_queue_db"), \
             patch.object(release, "exit_if_stale", side_effect=record_respawn_check):
            lane_queue._drain_locked(None, None, None, None, None, None, forever,
                                     B2B_BOUNDED_DRAIN_ITERATIONS)
        return len(respawn_checks)

    assert drive_bounded_drain(forever=True) == 1, "daemon drain did not check for respawn"
    assert drive_bounded_drain(forever=False) == 0, "bounded foreground drain checked for respawn"


def test_b2b_restart_request_never_polls_or_sleeps():
    consumer = {
        "pid": B2B_PID,
        "cmdline": "fixture",
        "identity_verified": True,
        "stale": True,
        "role": fleet.CONSUMER_ROLE_DAEMON,
        "supervision_mode": fleet.SUPERVISION_LAUNCHD,
        "supervision_ref": B2B_LABEL,
    }
    with patch.object(fleet, "running_consumers", return_value=[consumer]), \
         patch.object(fleet, "launchd_consumer_label_loaded", return_value=True), \
         patch.object(fleet, "kickstart_declared_launchd_consumer", return_value={"ok": True, "detail": ""}), \
         patch.object(release, "green_pointer", return_value={"sha": B2B_GREEN_SHA}), \
         patch("time.sleep", side_effect=AssertionError("blocked")):
        result = fleet.restart_stale(apply=True)
    assert result[fleet.FLEET_RESTART_REQUESTED_KEY] == [
        {"pid": B2B_PID, "launchd_label": B2B_LABEL, "target_sha": B2B_GREEN_SHA}
    ]
    assert not result[fleet.FLEET_RESTART_CONFIRMED_KEY]
    assert not result[fleet.FLEET_RESTART_FAILURE_KEY]
    assert not hasattr(fleet, "verify_launchd_consumer_green_return")


if __name__ == "__main__":
    test_b2b_install_kind_aware_respawn_is_loop_safe()
    test_b2b_self_respawn_decision_never_uses_cross_interpreter_subprocess()
    test_b2b_install_kind_ssot_agrees_in_process_and_cross_interpreter()
    test_b2b_self_install_kind_cache_does_not_cache_wheel_version()
    test_b2b_complete_supervised_daemon_inventory_has_respawn_safe_points()
    test_b2b_restart_request_never_polls_or_sleeps()
    print("[OK] B2b install-aware respawn, complete daemon wiring, and non-blocking restart proofs")
