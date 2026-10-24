"""Regression guards for bounded Claude spend surfaces above the global raw-row cap."""
import contextlib
import io
import os
import socket
import tempfile
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("SPENDGUARD_TEST_ISOLATED", "1")
os.environ.setdefault("SPENDGUARD_HOME", tempfile.mkdtemp())

import spendguard
from spendguard import calls, claudecode, config, guard, ledger_read, mcp_server
from spendguard.ledger import SpendLedger

GLOBAL_CAP_OVERFLOW_COUNT = 5
GLOBAL_CAP_LEDGER_ROW_COUNT = ledger_read.DEFAULT_LEDGER_READ_ROW_CAP + GLOBAL_CAP_OVERFLOW_COUNT
GLOBAL_CAP_CONVERSATION_COUNT = 4
GLOBAL_CAP_TOP_LIMIT = 3
GLOBAL_CAP_DAY = "2026-01-03"
GLOBAL_CAP_EVENT_SOURCE = "claude-code"
GLOBAL_CAP_PROVIDER = "anthropic"
GLOBAL_CAP_EST_VALUE = "1"
GLOBAL_CAP_DATABASE_NAME = "bounded-global-cap.sqlite"
GLOBAL_CAP_OVERAGE_WINDOW_START = 0.0
GLOBAL_CAP_OVERAGE_WINDOW_END = 4102444800.0
GLOBAL_CAP_INVOICE_SOURCE = "anthropic-invoice"
GLOBAL_CAP_INVOICE_INTENT = "anthropic-invoice:cc-overage:test"
GLOBAL_CAP_INVOICE_VALUE = "10"
GLOBAL_CAP_EVENT_ID_PREFIX = "event"
GLOBAL_CAP_INVOICE_ID = "invoice"
GLOBAL_CAP_DECISION_ID_PREFIX = "decision"
GLOBAL_CAP_DECISION_PROJECT = "project"
GLOBAL_CAP_DECISION_INTENT = "intent"
GLOBAL_CAP_DECISION_BASIS = "basis"
GLOBAL_CAP_REQUESTED_MODEL = "requested-model"
GLOBAL_CAP_REQUESTED_EFFORT = "requested-effort"
GLOBAL_CAP_CHOSEN_MODEL = "chosen-model"
GLOBAL_CAP_CHOSEN_EFFORT = "chosen-effort"
GLOBAL_CAP_COUNTERFACTUAL_USD = 1.0
GLOBAL_CAP_ACTUAL_USD = 0.0
GLOBAL_CAP_SAVED_USD = 1.0
GLOBAL_CAP_DECISION_REASON = "reason"
GLOBAL_CAP_DECISION_REASON_CLASS = "other"
GLOBAL_CAP_CALL_TIMESTAMP = "2026-01-03T00:00:00+00:00"
GLOBAL_CAP_CALL_PROVIDER = "openai"
GLOBAL_CAP_CALL_MODEL = "test-model"
GLOBAL_CAP_CALL_COST = 0.0
DISCARDED_EVIDENCE_MONTH = "2026-01"
DISCARDED_EVIDENCE_VALID_CONVERSATION = "valid-conversation"
DISCARDED_EVIDENCE_NON_NUMERIC_CONVERSATION = "non-numeric-conversation"
DISCARDED_EVIDENCE_NON_POSITIVE_CONVERSATION = "non-positive-conversation"
DISCARDED_EVIDENCE_VALID_VALUE = "4"
DISCARDED_EVIDENCE_NON_NUMERIC_VALUE = "not-a-number"
DISCARDED_EVIDENCE_NON_POSITIVE_VALUE = "0"
DISCARDED_EVIDENCE_INVOICE_VALUE = "12"
DISCARDED_EVIDENCE_EXPECTED_NON_NUMERIC_COUNT = 1
DISCARDED_EVIDENCE_EXPECTED_NON_POSITIVE_COUNT = 1


def deny_global_cap_socket(*args, **kwargs):
    raise OSError("bounded global-cap regression is offline: socket denied")


def seed_global_cap_ledger(path):
    writer = SpendLedger(path)
    event_rows = ((f"{GLOBAL_CAP_EVENT_ID_PREFIX}-{index}", GLOBAL_CAP_EVENT_SOURCE, GLOBAL_CAP_DAY,
                   GLOBAL_CAP_DAY, GLOBAL_CAP_EST_VALUE, str(index % GLOBAL_CAP_CONVERSATION_COUNT))
                  for index in range(GLOBAL_CAP_LEDGER_ROW_COUNT))
    writer._conn.executemany(
        "INSERT INTO spend_events(id,source,occurred_at,day,est_chat_usd,conv_id) VALUES (?,?,?,?,?,?)",
        event_rows)
    writer._conn.execute(
        "INSERT INTO spend_events(id,source,occurred_at,day,realtime_usd,intent) VALUES (?,?,?,?,?,?)",
        (GLOBAL_CAP_INVOICE_ID, GLOBAL_CAP_INVOICE_SOURCE, GLOBAL_CAP_DAY, GLOBAL_CAP_DAY,
         GLOBAL_CAP_INVOICE_VALUE, GLOBAL_CAP_INVOICE_INTENT))
    writer._conn.commit()
    writer._conn.close()
    with patch.object(config, "db_path", return_value=path):
        decision_connection = guard._decisions_db()
    calls._ensure_call_observation_schema(decision_connection)
    call_rows = ((GLOBAL_CAP_CALL_TIMESTAMP, GLOBAL_CAP_DECISION_INTENT, GLOBAL_CAP_CALL_PROVIDER,
                  GLOBAL_CAP_CALL_MODEL, GLOBAL_CAP_CALL_COST)
                 for _index in range(GLOBAL_CAP_LEDGER_ROW_COUNT))
    decision_connection.executemany(
        "INSERT INTO calls(ts,intent,provider,model,cost) VALUES (?,?,?,?,?)", call_rows)
    decision_rows = ((f"{GLOBAL_CAP_DECISION_ID_PREFIX}-{index}", GLOBAL_CAP_DAY,
                      GLOBAL_CAP_DECISION_PROJECT, GLOBAL_CAP_DECISION_INTENT, GLOBAL_CAP_DECISION_BASIS,
                      GLOBAL_CAP_REQUESTED_MODEL, GLOBAL_CAP_REQUESTED_EFFORT,
                      GLOBAL_CAP_CHOSEN_MODEL, GLOBAL_CAP_CHOSEN_EFFORT,
                      GLOBAL_CAP_COUNTERFACTUAL_USD, GLOBAL_CAP_ACTUAL_USD, GLOBAL_CAP_SAVED_USD,
                      GLOBAL_CAP_DECISION_REASON, GLOBAL_CAP_DECISION_REASON_CLASS)
                     for index in range(GLOBAL_CAP_LEDGER_ROW_COUNT))
    decision_connection.executemany(
        "INSERT INTO decisions(ts,day,project,intent,basis,requested_model,requested_effort,chosen_model,"
        "chosen_effort,counterfactual_usd,actual_usd,saved_usd,why,reason_class) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        decision_rows)
    decision_connection.commit()


def run_bounded_global_cap_guards():
    spendguard.require()
    with tempfile.TemporaryDirectory() as isolated_home:
        path = Path(isolated_home) / GLOBAL_CAP_DATABASE_NAME
        seed_global_cap_ledger(path)
        with patch.object(config, "db_path", return_value=path), \
                patch.object(claudecode, "_overage_events", return_value={}), \
                patch.object(claudecode, "_overage_windows", return_value=(
                    [(GLOBAL_CAP_OVERAGE_WINDOW_START, GLOBAL_CAP_OVERAGE_WINDOW_END)], None)):
            value_rows = ledger_read.read_cc_top_conversations(limit=GLOBAL_CAP_TOP_LIMIT, path=path)
            assert len(value_rows) <= GLOBAL_CAP_TOP_LIMIT

            overview = ledger_read.read_cc_spend_overview(path=path)
            assert overview["est"] == GLOBAL_CAP_LEDGER_ROW_COUNT

            guard._savings_db()
            overview_answer = mcp_server._tool_spend_overview({})
            assert overview_answer["est_value_usd"]["claude_code_plan_covered"] == GLOBAL_CAP_LEDGER_ROW_COUNT

            with contextlib.redirect_stdout(io.StringIO()):
                overage_answer = mcp_server._tool_top_conversations(
                    {"by": "overage", "limit": GLOBAL_CAP_TOP_LIMIT})
            assert len(overage_answer["conversations"]) <= GLOBAL_CAP_TOP_LIMIT

            try:
                ledger_read.read_spend_events(path=path)
            except ledger_read.LedgerReadTooLarge as error:
                assert error.cap == ledger_read.DEFAULT_LEDGER_READ_ROW_CAP
                assert error.total_at_least == ledger_read.DEFAULT_LEDGER_READ_ROW_CAP + 1
            else:
                raise AssertionError("unbounded raw ledger read must retain its oversize refusal")
            try:
                ledger_read.read_recent_calls(path=path)
            except ledger_read.LedgerReadTooLarge as error:
                assert error.cap == ledger_read.DEFAULT_LEDGER_READ_ROW_CAP
                assert error.total_at_least == ledger_read.DEFAULT_LEDGER_READ_ROW_CAP + 1
            else:
                raise AssertionError("unbounded raw calls read must retain its oversize refusal")


def run_discarded_attribution_evidence_guards():
    spendguard.require()
    real_rows = (ledger_read.LedgerReadRow(("mo", "billed"),
                                          (DISCARDED_EVIDENCE_MONTH, DISCARDED_EVIDENCE_INVOICE_VALUE)),)
    valid_row = ledger_read.LedgerReadRow(
        ("mo", "conv_id", "est"),
        (DISCARDED_EVIDENCE_MONTH, DISCARDED_EVIDENCE_VALID_CONVERSATION,
         DISCARDED_EVIDENCE_VALID_VALUE))
    rejected_rows = (
        ledger_read.LedgerReadRow(
            ("mo", "conv_id", "est"),
            (DISCARDED_EVIDENCE_MONTH, DISCARDED_EVIDENCE_NON_NUMERIC_CONVERSATION,
             DISCARDED_EVIDENCE_NON_NUMERIC_VALUE)),
        ledger_read.LedgerReadRow(
            ("mo", "conv_id", "est"),
            (DISCARDED_EVIDENCE_MONTH, DISCARDED_EVIDENCE_NON_POSITIVE_CONVERSATION,
             DISCARDED_EVIDENCE_NON_POSITIVE_VALUE)),
    )
    fallback_rows = ()

    def attributed_result_for_evidence(evidence_rows):
        with patch.object(claudecode, "_overage_events", return_value={}), \
                patch.object(claudecode, "_overage_windows", return_value=(
                    [(GLOBAL_CAP_OVERAGE_WINDOW_START, GLOBAL_CAP_OVERAGE_WINDOW_END)], None)), \
                patch.object(claudecode, "_sidebar_titles", return_value={}), \
                patch.object(ledger_read, "read_cc_attribution_evidence",
                             return_value=(real_rows, evidence_rows, fallback_rows)), \
                contextlib.redirect_stdout(io.StringIO()):
            return claudecode.attribute_overage(top=GLOBAL_CAP_TOP_LIMIT)

    clean_result = attributed_result_for_evidence((valid_row,))
    rejected_result = attributed_result_for_evidence((valid_row,) + rejected_rows)
    assert dict(rejected_result) == dict(clean_result)
    discarded_summary = rejected_result.discarded_evidence
    assert discarded_summary[claudecode.OVERAGE_DISCARD_COUNTS_FIELD] == {
        claudecode.OVERAGE_DISCARD_NON_NUMERIC_REASON: DISCARDED_EVIDENCE_EXPECTED_NON_NUMERIC_COUNT,
        claudecode.OVERAGE_DISCARD_NON_POSITIVE_REASON: DISCARDED_EVIDENCE_EXPECTED_NON_POSITIVE_COUNT,
    }
    assert discarded_summary[claudecode.OVERAGE_DISCARD_SAMPLE_FIELD] == [
        {
            claudecode.OVERAGE_DISCARD_MONTH_FIELD: DISCARDED_EVIDENCE_MONTH,
            claudecode.OVERAGE_DISCARD_CONVERSATION_FIELD: DISCARDED_EVIDENCE_NON_NUMERIC_CONVERSATION,
            claudecode.OVERAGE_DISCARD_REASON_FIELD: claudecode.OVERAGE_DISCARD_NON_NUMERIC_REASON,
        },
        {
            claudecode.OVERAGE_DISCARD_MONTH_FIELD: DISCARDED_EVIDENCE_MONTH,
            claudecode.OVERAGE_DISCARD_CONVERSATION_FIELD: DISCARDED_EVIDENCE_NON_POSITIVE_CONVERSATION,
            claudecode.OVERAGE_DISCARD_REASON_FIELD: claudecode.OVERAGE_DISCARD_NON_POSITIVE_REASON,
        },
    ]


if __name__ == "__main__":
    with patch.object(socket.socket, "connect", deny_global_cap_socket), \
            patch.object(socket.socket, "connect_ex", deny_global_cap_socket), \
            patch.object(socket, "create_connection", deny_global_cap_socket):
        run_bounded_global_cap_guards()
        run_discarded_attribution_evidence_guards()
    print("bounded overage/overview global-cap guards: OK")
