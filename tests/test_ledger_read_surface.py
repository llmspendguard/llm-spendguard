"""Offline script guards for the single bounded, read-only consumer surface."""
import dataclasses
import contextlib
import io
import inspect
import itertools
import json
import os
import socket
import sqlite3
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("SPENDGUARD_TEST_ISOLATED", "1")
os.environ.setdefault("SPENDGUARD_HOME", tempfile.mkdtemp())

import spendguard
from spendguard import config, ledger_read
from spendguard.ledger import SpendLedger
from spendguard import calls, cli, mcp_server, receipt

SURFACE_SEED_COUNT = 5
SURFACE_TEST_CAP = 2
SURFACE_TEST_BUDGET_S = 1.0
SURFACE_PROGRESS_STEP = 1
SURFACE_SEED_START = 1
SURFACE_EXPECTED_CALLS = 1
SURFACE_EXPECTED_GROUPS = 1
SURFACE_SEED_DAY = "2026-01-03"
SURFACE_SEED_SOURCE = "ledger-read-characterization"
SURFACE_SEED_PROVIDER = "openai"
SURFACE_TEST_DATABASE = "ledger-read.sqlite"
SURFACE_EMPTY_DATABASE = "ledger-empty.sqlite"
SURFACE_RECURSIVE_SQL = """WITH RECURSIVE counter(value) AS
    (SELECT 1 UNION ALL SELECT value + 1 FROM counter WHERE value < ?)
    SELECT SUM(value) AS total FROM counter"""
SURFACE_RECURSIVE_STEPS = 100000
SURFACE_FORMATTED_MONEY = "receipt-ssot-money"
SURFACE_ESTIMATED_VALUE = "2.0"
SURFACE_SEED_INTENT = "surface-characterization"


def deny_surface_test_socket(*args, **kwargs):
    raise OSError("ledger surface tests are offline: socket denied")


def check_surface_guard(label, condition, failures):
    print(f"[{'OK' if condition else 'FAIL'}] {label}")
    if not condition:
        failures.append(label)


def run_surface_guards(failures):
    spendguard.require()
    with tempfile.TemporaryDirectory() as surface_directory:
        path = Path(surface_directory) / SURFACE_TEST_DATABASE
        writer = SpendLedger(path)
        identities = [writer.record_event({
            "source": SURFACE_SEED_SOURCE, "kind": "realtime", "usd": index + SURFACE_SEED_START,
            "conv_id": str(index), "attr_what": str(index),
            "occurred_at": SURFACE_SEED_DAY, "provider": SURFACE_SEED_PROVIDER,
            "est_chat_usd": SURFACE_ESTIMATED_VALUE,
        }) for index in range(SURFACE_SEED_COUNT)]
        expected = [dict(row) for row in writer._conn.execute(
            "SELECT id, day, source, realtime_usd FROM spend_events ORDER BY id")]
        expected_compatibility = [writer._row(row) for row in writer._conn.execute(
            "SELECT " + ", ".join(ledger_read.SPEND_EVENT_READ_COLUMNS) + " FROM spend_events ORDER BY day, id")]
        check_surface_guard("SpendLedger query delegates without changing seeded values",
                            writer.query() == expected_compatibility, failures)
        check_surface_guard("SpendLedger category delegates without changing seeded values",
                            writer.in_category("realtime") == expected_compatibility, failures)
        expected_total = sum(range(SURFACE_SEED_START, SURFACE_SEED_COUNT + SURFACE_SEED_START))
        grouped_total = writer.rollup("provider")
        check_surface_guard("grouped rollup retains exact seeded dimensions and money totals",
                            set(grouped_total) == {SURFACE_SEED_PROVIDER}
                            and grouped_total[SURFACE_SEED_PROVIDER]["realtime_usd"] == expected_total
                            and grouped_total[SURFACE_SEED_PROVIDER]["est_chat_usd"] == SURFACE_SEED_COUNT * float(SURFACE_ESTIMATED_VALUE)
                            and grouped_total[SURFACE_SEED_PROVIDER]["billed_usd"] == expected_total, failures)
        calls._ensure_calls_schema(writer._conn)
        writer._conn.execute("INSERT INTO calls(ts,intent,provider,model,cost) VALUES (?,?,?,?,?)",
                             (SURFACE_SEED_DAY, SURFACE_SEED_INTENT, SURFACE_SEED_PROVIDER, SURFACE_SEED_SOURCE, SURFACE_ESTIMATED_VALUE))
        writer._conn.commit()
        writer._conn.close()
        baseline = config.ledger_connection_count()
        request = ledger_read.LedgerReadRequest(
            columns=("id", "day", "source", "realtime_usd"), order_by=("id",))
        actual = ledger_read.read_ledger_rows(request, path=path)
        check_surface_guard("rows are typed dataclasses, never raw tuples", bool(actual) and all(
            dataclasses.is_dataclass(row) and not isinstance(row, (tuple, sqlite3.Row))
            for row in actual), failures)
        check_surface_guard("projected values equal seeded legacy SELECT", [dict(row) for row in actual] == expected,
                            failures)
        too_small = dataclasses.replace(request, row_cap=SURFACE_TEST_CAP)
        try:
            ledger_read.read_ledger_rows(too_small, path=path)
        except ledger_read.LedgerReadTooLarge as error:
            check_surface_guard("row cap is fail-loud with cap and at-least count",
                                error.cap == SURFACE_TEST_CAP and error.total_at_least > error.cap, failures)
        else:
            check_surface_guard("row cap is fail-loud with cap and at-least count", False, failures)
        count = ledger_read.read_ledger_count(too_small, path=path)
        check_surface_guard("count returns true total beyond row cap", count == SURFACE_SEED_COUNT, failures)
        filtered_count = ledger_read.read_ledger_count(dataclasses.replace(
            too_small, where_sql="source = ?", parameters=(SURFACE_SEED_SOURCE,)), path=path)
        check_surface_guard("count applies typed request filters", filtered_count == SURFACE_SEED_COUNT, failures)
        call_rows = ledger_read.read_recent_calls(path=path)
        check_surface_guard("recent calls return projected typed evidence",
                            len(call_rows) == SURFACE_EXPECTED_CALLS
                            and dataclasses.is_dataclass(call_rows[0])
                            and call_rows[0]["intent"] == SURFACE_SEED_INTENT, failures)
        aggregates = ledger_read.read_spend_aggregates(path=path)
        check_surface_guard("aggregates preserve exact separate money axes",
                            len(aggregates) == SURFACE_EXPECTED_GROUPS
                            and aggregates[0]["n"] == SURFACE_SEED_COUNT
                            and float(aggregates[0]["est_chat_usd"]) == SURFACE_SEED_COUNT * float(SURFACE_ESTIMATED_VALUE)
                            and float(aggregates[0]["realtime_usd"]) == expected_total, failures)
        try:
            with ledger_read._ledger_read_connection(path, SURFACE_TEST_BUDGET_S) as connection:
                connection.execute("DELETE FROM spend_events")
        except sqlite3.OperationalError:
            check_surface_guard("read-only SQLite handle refuses writes", True, failures)
        else:
            check_surface_guard("read-only SQLite handle refuses writes", False, failures)
        empty_path = Path(surface_directory) / SURFACE_EMPTY_DATABASE
        empty_connection = config.open_ledger_connection(empty_path)
        empty_connection.close()
        try:
            ledger_read.read_ledger_rows(request, path=empty_path)
        except ledger_read.LedgerReadSchemaError:
            check_surface_guard("missing table raises typed schema error", True, failures)
        else:
            check_surface_guard("missing table raises typed schema error", False, failures)
        ticks = itertools.count()
        with patch.object(ledger_read, "DEFAULT_LEDGER_READ_PROGRESS_VM_INSTRUCTIONS", SURFACE_PROGRESS_STEP), \
                patch.object(ledger_read.time, "monotonic", side_effect=lambda: float(next(ticks))):
            try:
                ledger_read.read_ledger_sql(ledger_read.LedgerSQLReadRequest(
                    SURFACE_RECURSIVE_SQL, (SURFACE_RECURSIVE_STEPS,), time_budget=SURFACE_TEST_BUDGET_S), path=path)
            except ledger_read.LedgerReadTimeout as error:
                check_surface_guard("progress budget raises typed timeout naming budget",
                                    str(SURFACE_TEST_BUDGET_S) in str(error), failures)
            else:
                check_surface_guard("progress budget raises typed timeout naming budget", False, failures)
        sql_statements = []
        original_opener = config.open_ledger_connection

        def trace_surface_open(*args, **kwargs):
            connection = original_opener(*args, **kwargs)
            connection.set_trace_callback(sql_statements.append)
            return connection

        with patch.object(config, "open_ledger_connection", side_effect=trace_surface_open):
            ledger_read.read_ledger_rows(request, path=path)
        check_surface_guard("surface selects named columns", bool(sql_statements) and all(
            "SELECT *" not in statement.upper() for statement in sql_statements), failures)
        check_surface_guard("all successful and failed reads release admission slots",
                            config.ledger_connection_count() == baseline, failures)
        check_surface_guard("by-id event reads preserve seed identity",
                            set(identities) == {row["id"] for row in actual}
                            and ledger_read.read_ledger_event(identities[0], path=path)["id"] == identities[0], failures)
        with patch.object(receipt, "_money", return_value=SURFACE_FORMATTED_MONEY) as money_formatter:
            mcp_answer = mcp_server._tool_ledger_read({"operation": "event", "event_id": identities[0], "path": str(path)})
            cli_stdout = io.StringIO()
            with contextlib.redirect_stdout(cli_stdout):
                cli_result = cli.ledger_read_cli(["event", "--event-id", identities[0], "--path", str(path)])
            check_surface_guard("CLI and MCP return identical shared library answers",
                                cli_result == 0 and json.loads(cli_stdout.getvalue()) == mcp_answer, failures)
            money_row = mcp_answer["rows"][0]
            check_surface_guard("money rendering reuses receipt SSOT and keeps cost axes separate",
                                money_formatter.called
                                and money_row["realtime_usd"] == SURFACE_FORMATTED_MONEY
                                and money_row["est_chat_usd"] == SURFACE_FORMATTED_MONEY
                                and "total_usd" not in money_row, failures)
    check_surface_guard("surface source contains no wildcard SELECT", "SELECT *" not in inspect.getsource(ledger_read).upper(),
                        failures)


if __name__ == "__main__":
    surface_failures = []
    with patch.object(socket.socket, "connect", deny_surface_test_socket), \
            patch.object(socket.socket, "connect_ex", deny_surface_test_socket), \
            patch.object(socket, "create_connection", deny_surface_test_socket):
        run_surface_guards(surface_failures)
    print(f"ledger read surface: {len(surface_failures)} failure(s)")
    sys.exit(bool(surface_failures))
