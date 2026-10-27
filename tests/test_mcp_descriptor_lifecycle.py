"""Offline descriptor policy, recovery, and fail-closed restart guards."""
from unittest.mock import Mock, patch

from spendguard import mcp_fd_lifecycle as lifecycle
from spendguard import mcp_server

TEST_DESCRIPTOR_SOFT = 100
TEST_DESCRIPTOR_HARD = 200
TEST_DESCRIPTOR_TARGET = 150
TEST_DESCRIPTOR_FRACTION = 0.5
TEST_DESCRIPTOR_HIGH = 60
TEST_DESCRIPTOR_RECOVERED = 20


class DescriptorProbeHTTPServer:
    """Exercise the idle service hook without opening a network listener."""

    closed = False
    service_checked = False

    def __init__(self, address, handler):
        self.address = address

    def __enter__(self):
        return self

    def __exit__(self, *exception):
        DescriptorProbeHTTPServer.closed = True

    def serve_forever(self, poll_interval):
        DescriptorProbeHTTPServer.service_checked = True
        self.service_actions()


def verify_idle_mcp_pressure_stops_serving():
    with patch("http.server.ThreadingHTTPServer", DescriptorProbeHTTPServer), \
         patch.object(mcp_server, "build_mcp_http_handler"), \
         patch.object(mcp_server, "_prewarm_codex_daemon"), \
         patch("spendguard.fleet.register_consumer"), \
         patch.object(lifecycle, "raise_mcp_descriptor_soft_limit") as raise_limit, \
         patch.object(lifecycle, "enforce_mcp_descriptor_headroom", side_effect=
                      lifecycle.MCPDescriptorPressureError("descriptor probe")) as check:
        mcp_server.serve_http()
        raise_limit.assert_called_once()
        check.assert_called_once()
        assert DescriptorProbeHTTPServer.service_checked and DescriptorProbeHTTPServer.closed
    print("[OK] idle HTTP daemon closes its server on unrecovered pressure")


def verify_mcp_descriptor_lifecycle():
    with patch.object(lifecycle.resource, "getrlimit", side_effect=[
        (TEST_DESCRIPTOR_SOFT, TEST_DESCRIPTOR_HARD),
        (TEST_DESCRIPTOR_TARGET, TEST_DESCRIPTOR_HARD),
    ]), patch.object(lifecycle.resource, "setrlimit") as set_limit:
        assert lifecycle.raise_mcp_descriptor_soft_limit(TEST_DESCRIPTOR_TARGET) == TEST_DESCRIPTOR_TARGET
        set_limit.assert_called_once_with(lifecycle.resource.RLIMIT_NOFILE,
                                          (TEST_DESCRIPTOR_TARGET, TEST_DESCRIPTOR_HARD))
    with patch.object(lifecycle.resource, "getrlimit", return_value=(TEST_DESCRIPTOR_SOFT, TEST_DESCRIPTOR_HARD)), \
         patch.object(lifecycle.psutil, "Process") as process_factory, \
         patch.object(lifecycle.config, "recycle_idle_ledger_connections", create=True) as recycle:
        process_factory.return_value.num_fds = Mock(side_effect=[TEST_DESCRIPTOR_HIGH, TEST_DESCRIPTOR_RECOVERED])
        lifecycle.enforce_mcp_descriptor_headroom(TEST_DESCRIPTOR_FRACTION)
        recycle.assert_called_once()
        process_factory.return_value.num_fds = Mock(return_value=TEST_DESCRIPTOR_HIGH)
        try:
            lifecycle.enforce_mcp_descriptor_headroom(TEST_DESCRIPTOR_FRACTION)
        except lifecycle.MCPDescriptorPressureError:
            pass
        else:
            raise AssertionError("persistent pressure must stop HTTP serving")
    with patch.object(lifecycle.config, "_cfg_get", return_value=0):
        try:
            lifecycle.mcp_descriptor_policy()
        except ValueError:
            pass
        else:
            raise AssertionError("invalid policy must fail closed")
    print("[OK] MCP descriptor startup, recycle, and fail-closed restart")


if __name__ == "__main__":
    verify_mcp_descriptor_lifecycle()
    verify_idle_mcp_pressure_stops_serving()
