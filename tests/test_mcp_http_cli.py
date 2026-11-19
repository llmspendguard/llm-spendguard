"""Offline CLI dispatch checks for HTTP daemon installation and migration."""
import contextlib
import io
import json
from unittest.mock import patch

from spendguard.interface import cli

CLI_FIXTURE_PORT = 24567


def test_mcp_http_lifecycle_cli_dispatch():
    output = io.StringIO()
    with patch("spendguard.platform.mcp_http_launchd.install_mcp_http_launchd_daemon", return_value={"status": "installed"}) as install, contextlib.redirect_stdout(output):
        assert cli._dispatch(["mcp", "--install-http", "--port", str(CLI_FIXTURE_PORT)]) == 0
    install.assert_called_once_with(port=CLI_FIXTURE_PORT)
    assert json.loads(output.getvalue())["status"] == "installed"

    for arguments, apply in ((["--migrate-http"], False), (["--migrate-http", "--apply"], True)):
        output = io.StringIO()
        with patch("spendguard.platform.mcp_http_launchd.migrate_mcp_http_to_package_owned", return_value={"migrated": apply}) as migrate, contextlib.redirect_stdout(output):
            assert cli._dispatch(["mcp", *arguments]) == 0
        migrate.assert_called_once_with(apply=apply)
        assert json.loads(output.getvalue())["migrated"] is apply


if __name__ == "__main__":
    test_mcp_http_lifecycle_cli_dispatch()
    print("MCP HTTP CLI dispatch: passed")
