"""Library, CLI and MCP parity for sanctioned ledger consumers; offline and isolated."""
import contextlib
import io
import json
import os
import sqlite3
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("SPENDGUARD_HOME", tempfile.mkdtemp())
os.environ.setdefault("SPENDGUARD_TEST_ISOLATED", "1")

import spendguard
from spendguard import budget, cli, config, ledger_read, mcp_server

CONSUMER_TRANSPORT_DAY = "2026-01-03"
CONSUMER_TRANSPORT_CONVERSATION = "consumer-transport-conversation"
CONSUMER_TRANSPORT_COSTS = (1.25, 2.5)
CONSUMER_TRANSPORT_DATABASE = "transport.sqlite"
CONSUMER_TRANSPORT_DEADLINE_S = 1.0
CONSUMER_TRANSPORT_REQUEST_ID = "consumer-parity"
CONSUMER_TRANSPORT_MODEL = "consumer-transport-model"
CONSUMER_TRANSPORT_PROVIDER = "consumer-transport-provider"
CONSUMER_TRANSPORT_KIND = "realtime"


def consumer_transport_mcp_result(arguments):
    response = mcp_server.handle({"jsonrpc": "2.0", "id": CONSUMER_TRANSPORT_REQUEST_ID,
        "method": "tools/call", "params": {"name": "spendguard_ledger_read", "arguments": arguments}})
    return response["result"]


def consumer_transport_cli_result(arguments):
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        status = cli.ledger_read_cli(arguments + ["--json"])
    return status, json.loads(output.getvalue())


def run_consumer_transport_parity():
    spendguard.require()
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / CONSUMER_TRANSPORT_DATABASE
        connection = sqlite3.connect(path)
        connection.execute("CREATE TABLE calls(cost REAL)")
        connection.executemany("INSERT INTO calls VALUES (?)", [(cost,) for cost in CONSUMER_TRANSPORT_COSTS])
        connection.commit()
        connection.close()
        expected = {"recorded_call_total": ledger_read.read_recorded_call_total(path)}
        status, answer = consumer_transport_cli_result(["recorded-call-total", "--path", str(path)])
        mcp = consumer_transport_mcp_result({"operation": "recorded-call-total", "path": str(path)})
        assert status == 0 and answer == expected == mcp["structuredContent"] and not mcp["isError"]
        path.unlink()
        status, answer = consumer_transport_cli_result(["recorded-call-total", "--path", str(path)])
        mcp = consumer_transport_mcp_result({"operation": "recorded-call-total", "path": str(path)})
        assert status != 0 and "recorded_call_total" not in answer and mcp["isError"]
        assert answer == mcp["structuredContent"]

    # Seed the actual aggregate's input through its existing sanctioned writer, never a real ledger.
    for cost in CONSUMER_TRANSPORT_COSTS:
        budget.record_charge(CONSUMER_TRANSPORT_PROVIDER, CONSUMER_TRANSPORT_MODEL,
                             CONSUMER_TRANSPORT_KIND, cost, conv_id=CONSUMER_TRANSPORT_CONVERSATION)
    expected_spend = ledger_read.read_conversation_spend_since(
        CONSUMER_TRANSPORT_DAY, conv=CONSUMER_TRANSPORT_CONVERSATION, deadline_s=CONSUMER_TRANSPORT_DEADLINE_S)
    arguments = {"operation": "conversation-spend-since", "since": CONSUMER_TRANSPORT_DAY,
                 "conv": CONSUMER_TRANSPORT_CONVERSATION, "deadline_s": CONSUMER_TRANSPORT_DEADLINE_S}
    status, answer = consumer_transport_cli_result(["conversation-spend-since", "--since", CONSUMER_TRANSPORT_DAY,
        "--conv", CONSUMER_TRANSPORT_CONVERSATION, "--deadline-s", str(CONSUMER_TRANSPORT_DEADLINE_S)])
    assert status == 0 and answer == {"conversation_spend_since": expected_spend}
    assert consumer_transport_mcp_result(arguments)["structuredContent"] == answer
    # Omission must leave the library's config-backed default intact; explicit values pass unchanged.
    with patch.object(ledger_read, "read_conversation_spend_since", return_value=expected_spend) as forwarded:
        consumer_transport_cli_result(["conversation-spend-since", "--since", CONSUMER_TRANSPORT_DAY,
            "--conv", CONSUMER_TRANSPORT_CONVERSATION])
        forwarded.assert_called_once_with(CONSUMER_TRANSPORT_DAY, conv=CONSUMER_TRANSPORT_CONVERSATION)
        forwarded.reset_mock()
        consumer_transport_mcp_result({key: value for key, value in arguments.items() if key != "deadline_s"})
        forwarded.assert_called_once_with(CONSUMER_TRANSPORT_DAY, conv=CONSUMER_TRANSPORT_CONVERSATION)
        forwarded.reset_mock()
        consumer_transport_cli_result(["conversation-spend-since", "--since", CONSUMER_TRANSPORT_DAY,
            "--conv", CONSUMER_TRANSPORT_CONVERSATION, "--deadline-s", str(CONSUMER_TRANSPORT_DEADLINE_S)])
        forwarded.assert_called_once_with(CONSUMER_TRANSPORT_DAY, conv=CONSUMER_TRANSPORT_CONVERSATION,
                                         deadline_s=CONSUMER_TRANSPORT_DEADLINE_S)
        forwarded.reset_mock()
        consumer_transport_mcp_result(arguments)
        forwarded.assert_called_once_with(CONSUMER_TRANSPORT_DAY, conv=CONSUMER_TRANSPORT_CONVERSATION,
                                         deadline_s=CONSUMER_TRANSPORT_DEADLINE_S)
    with patch.object(budget, "spent_since", side_effect=ledger_read.LedgerQueryDeadline("injected deadline")):
        status, answer = consumer_transport_cli_result(["conversation-spend-since", "--since", CONSUMER_TRANSPORT_DAY,
            "--conv", CONSUMER_TRANSPORT_CONVERSATION])
        mcp = consumer_transport_mcp_result(arguments)
        assert status != 0 and mcp["isError"] and answer == mcp["structuredContent"]
        assert answer["error_type"] == "LedgerQueryDeadline"
    metrics = ledger_read.ledger_read_metrics()
    status, answer = consumer_transport_cli_result(["metrics"])
    assert status == 0 and answer == metrics
    assert consumer_transport_mcp_result({"operation": "metrics"})["structuredContent"] == metrics
    assert metrics["open_connections"] == config.ledger_connection_count()


if __name__ == "__main__":
    run_consumer_transport_parity()
    print("consumer surface transports: all parity and failure guards passed")
    sys.exit(0)
