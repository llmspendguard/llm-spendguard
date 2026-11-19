"""Install-aware release identity and idle MCP recovery without a live service."""
import io
import os
import threading
from unittest.mock import patch

from spendguard.interface import mcp_server
from spendguard.platform import manifest, release


def test_editable_freshness_uses_checkout_head_instead_of_pointer():
    loaded = "loaded-commit"
    pointer = "different-green-commit"
    with patch.object(manifest, "inspect_current_interpreter_install_kind",
                      return_value={"install_kind": manifest.MANIFEST_EDITABLE_KIND}), \
         patch.object(release, "served_sha", return_value={"sha": loaded}), \
         patch.object(release, "green_pointer", return_value={"sha": pointer}), \
         patch.object(release, "_git_src", return_value=loaded):
        status = release.release_status()
        assert status["stale"] is False
        assert status["green"]["sha"] == pointer
        assert status["freshness"]["source_sha"] == loaded
        assert release.should_respawn() is False
    with patch.object(manifest, "inspect_current_interpreter_install_kind",
                      return_value={"install_kind": manifest.MANIFEST_EDITABLE_KIND}), \
         patch.object(release, "served_sha", return_value={"sha": loaded}), \
         patch.object(release, "_git_src", return_value=pointer):
        assert release.should_respawn() is True
        try:
            release.exit_if_stale()
        except SystemExit as exc:
            assert exc.code == 0
        else:
            raise AssertionError("stale editable process did not exit")


def test_idle_stdio_rechecks_freshness_without_a_request(monkeypatch, partial_frame=b""):
    read_fd, write_fd = os.pipe()
    checked = threading.Event()
    monkeypatch.setattr(mcp_server, "MCP_STDIO_FRESHNESS_POLL_SECONDS", 0.01)
    monkeypatch.setattr(mcp_server, "_arm_and_verify_mcp_daemon_gate", lambda: None)
    monkeypatch.setenv("SPENDGUARD_MCP_AUTO_RESPAWN", "1")
    from spendguard.platform import platform_accessors
    monkeypatch.setattr(platform_accessors, "resolve_register_consumer", lambda **kwargs: None)

    def mark_stdio_freshness_check():
        checked.set()
        return True

    monkeypatch.setattr(platform_accessors, "resolve_should_respawn", mark_stdio_freshness_check)
    try:
        if partial_frame:
            os.write(write_fd, partial_frame)
        with os.fdopen(read_fd, "r") as inp:
            mcp_server.serve_stdio(inp, io.StringIO())
        assert checked.is_set()
    finally:
        os.close(write_fd)


if __name__ == "__main__":
    from pytest import MonkeyPatch

    test_editable_freshness_uses_checkout_head_instead_of_pointer()
    with MonkeyPatch.context() as patcher:
        test_idle_stdio_rechecks_freshness_without_a_request(patcher)
    with MonkeyPatch.context() as patcher:
        test_idle_stdio_rechecks_freshness_without_a_request(patcher, b'{"jsonrpc":')
    print("[OK] editable source freshness and idle stdio recovery")
