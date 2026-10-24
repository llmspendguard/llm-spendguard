"""Regression: MCP daemon startup self-arms before a spending HTTP tool is dispatched.

Hermetic: the real HTTP handler runs over a socket pair; the recommendation is estimate-only and refused by a
zero budget before any LLM call. No port, network request, daemon, or paid model is used.
"""
import functools
import http.client
import json
import os
import socket
import sys
import tempfile
import types
from contextlib import nullcontext

NO_AUTOINSTALL_ENVIRONMENT_KEY = "SPENDGUARD_NO_AUTOINSTALL"
NO_AUTOINSTALL_ENABLED_VALUE = "1"
TEST_ISOLATION_ENVIRONMENT_KEY = "SPENDGUARD_TEST_ISOLATED"
TEST_ISOLATION_ENABLED_VALUE = "1"
DAEMON_GATE_TEST_BOOTSTRAPPED_ENVIRONMENT_KEY = "SPENDGUARD_MCP_GATE_TEST_BOOTSTRAPPED"
TEST_HOME_ENVIRONMENT_KEY = "SPENDGUARD_HOME"
TEST_HOME_PREFIX = "spendguard-mcp-gate-startup-"
MCP_PATH = "/mcp"
JSON_RPC_VERSION = "2.0"
SPENDING_TOOL_NAME = "spendguard_recommend"
TEST_INTENT = "mcp-daemon-gate-startup"
ZERO_SPEND_BUDGET_USD = 0.0
REGRESSION_ESTIMATED_REQUEST_COUNT = 1
REGRESSION_ESTIMATED_COST_USD = 1.0
HTTP_OK_STATUS = 200
IN_PROCESS_PEER_ADDRESS = ("in-process", 0)
HTTP_CONTENT_TYPE_HEADER = "Content-Type"
HTTP_CONTENT_LENGTH_HEADER = "Content-Length"

if os.environ.get(DAEMON_GATE_TEST_BOOTSTRAPPED_ENVIRONMENT_KEY) != TEST_ISOLATION_ENABLED_VALUE:
    os.environ[DAEMON_GATE_TEST_BOOTSTRAPPED_ENVIRONMENT_KEY] = TEST_ISOLATION_ENABLED_VALUE
    os.environ[NO_AUTOINSTALL_ENVIRONMENT_KEY] = NO_AUTOINSTALL_ENABLED_VALUE
    os.environ[TEST_ISOLATION_ENVIRONMENT_KEY] = TEST_ISOLATION_ENABLED_VALUE
    os.environ[TEST_HOME_ENVIRONMENT_KEY] = tempfile.mkdtemp(prefix=TEST_HOME_PREFIX)
    os.execv(sys.executable, [sys.executable] + sys.argv)
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from spendguard import advisor, gate, ledger_read, mcp_server  # noqa: E402

CORE_GATE_INTERCEPTOR_SPECS = (
    gate.INTERCEPTORS + gate._EXTRA + gate.RT_INTERCEPTORS + gate.STREAM_INTERCEPTORS + gate.UNIT_INTERCEPTORS
)
CORE_GATE_SDK_PACKAGES = sorted({spec[0].partition(".")[0] for spec in CORE_GATE_INTERCEPTOR_SPECS})
IN_PROCESS_LEDGER_REQUEST_READS = types.SimpleNamespace(cancel_ledger_request=lambda: None)


def record_mcp_daemon_gate_check(check_failures, label, condition):
    print(f"  [{'OK' if condition else 'FAIL'}] {label}")
    if not condition:
        check_failures.append(label)


def post_mcp_request_over_socket_pair(handler_class, payload):
    request_body = json.dumps(payload).encode("utf-8")
    request_bytes = (
        f"POST {MCP_PATH} HTTP/1.1\r\n".encode("ascii")
        + b"Host: in-process\r\n"
        + f"{HTTP_CONTENT_TYPE_HEADER}: application/json\r\n".encode("ascii")
        + f"{HTTP_CONTENT_LENGTH_HEADER}: {len(request_body)}\r\n".encode("ascii")
        + b"Connection: close\r\n\r\n"
        + request_body
    )
    client_socket, server_socket = socket.socketpair()
    try:
        client_socket.sendall(request_bytes)
        client_socket.shutdown(socket.SHUT_WR)
        handler_class(server_socket, IN_PROCESS_PEER_ADDRESS, object())
        response = http.client.HTTPResponse(client_socket)
        response.begin()
        return response.status, json.loads(response.read())
    finally:
        server_socket.close()
        client_socket.close()


def record_explicit_daemon_gate_install(gate_install_call_records, delegated_gate_install, *args, **kwargs):
    gate_install_call_records.append((args, kwargs))
    return delegated_gate_install(*args, **kwargs)


def return_mcp_gate_regression_recommendation_estimate(**_kwargs):
    return {"estimate_only": True, "requests": REGRESSION_ESTIMATED_REQUEST_COUNT,
            "cost": REGRESSION_ESTIMATED_COST_USD, "candidates": []}


def report_framework_startup_enforcement_state(original_enforcement_probe):
    """Reproduce framework Python: live hooks exist, but enforcement is false until a core SDK is imported."""
    if not any(sdk_package in sys.modules for sdk_package in CORE_GATE_SDK_PACKAGES):
        return False
    return original_enforcement_probe()


def run_mcp_daemon_gate_startup_regression():
    check_failures = []
    gate_install_call_records = []
    original_gate_install = gate.install
    original_enforcement_probe = gate._enforcement_armed
    record_mcp_daemon_gate_check(
        check_failures,
        "framework startup begins with a live hook and no imported core SDK",
        not any(sdk_package in sys.modules for sdk_package in CORE_GATE_SDK_PACKAGES),
    )
    gate.install = functools.partial(
        record_explicit_daemon_gate_install,
        gate_install_call_records,
        original_gate_install,
    )
    gate._enforcement_armed = functools.partial(
        report_framework_startup_enforcement_state,
        original_enforcement_probe,
    )
    try:
        http_handler_class = mcp_server.build_mcp_http_handler()
    finally:
        gate.install = original_gate_install
        gate._enforcement_armed = original_enforcement_probe

    record_mcp_daemon_gate_check(
        check_failures,
        "startup explicitly installs and arms the gate even with SPENDGUARD_NO_AUTOINSTALL=1",
        len(gate_install_call_records) == 1
        and os.environ[NO_AUTOINSTALL_ENVIRONMENT_KEY] == NO_AUTOINSTALL_ENABLED_VALUE
        and gate._enforcement_armed(),
    )

    request = {
        "jsonrpc": JSON_RPC_VERSION,
        "id": 1,
        "method": "tools/call",
        "params": {
            "name": SPENDING_TOOL_NAME,
            "arguments": {"intent": TEST_INTENT, "budget_usd": ZERO_SPEND_BUDGET_USD},
        },
    }
    original_recommend_models = advisor.recommend_models
    original_ledger_request_scope = ledger_read.ledger_request_scope
    advisor.recommend_models = return_mcp_gate_regression_recommendation_estimate
    ledger_read.ledger_request_scope = functools.partial(
        nullcontext,
        enter_result=IN_PROCESS_LEDGER_REQUEST_READS,
    )
    try:
        http_status, response_payload = post_mcp_request_over_socket_pair(http_handler_class, request)
    finally:
        advisor.recommend_models = original_recommend_models
        ledger_read.ledger_request_scope = original_ledger_request_scope
    tool_result = response_payload.get("result", {})
    serialized_response = json.dumps(response_payload)
    spending_tool_returned_normal_result = (
        http_status == HTTP_OK_STATUS
        and tool_result.get("isError") is False
        and "SpendGateRefused" not in serialized_response
    )
    record_mcp_daemon_gate_check(
        check_failures,
        "spending HTTP tool returns a normal result rather than SpendGateRefused",
        spending_tool_returned_normal_result,
    )
    if not spending_tool_returned_normal_result:
        print(f"    response: HTTP {http_status} {serialized_response}")

    print(
        f"\n{'[FAIL]' if check_failures else 'OK'} test_mcp_daemon_gate_startup: "
        f"{len(check_failures)} failure(s)"
    )
    return 1 if check_failures else 0


raise SystemExit(run_mcp_daemon_gate_startup_regression())
