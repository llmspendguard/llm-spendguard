"""All-row residency accounting beyond the bounded reader, with a corrupt-row fault.

Offline script guard: a full-scan reference independently prices every seed row.
The renderer must remain numeric beyond the old cap and explicitly name lost evidence.
"""
import math
import os
from pathlib import Path
import sqlite3
import tempfile
from unittest.mock import patch

os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"
os.environ["SPENDGUARD_NO_AUTOINSTALL"] = "1"
os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="sg-residency-containment-")

from spendguard import claudecode, config, ledger_read, pricing  # noqa: E402

RESIDENCY_CONTAINMENT_MODELS = ("claude-opus-4-8", "claude-sonnet-4-6")
RESIDENCY_CONTAINMENT_SESSION = "residency-containment-seed"
RESIDENCY_CONTAINMENT_SOURCE = "claude-code"
RESIDENCY_CONTAINMENT_DB_NAME = "residency-containment.sqlite"
RESIDENCY_CONTAINMENT_EXTRA_ROWS = 7
RESIDENCY_CONTAINMENT_PRECISION = 0.0002
RESIDENCY_CONTAINMENT_INSTRUCTIONS = 500
RESIDENCY_CONTAINMENT_TOOL_TEXT = "whole evidence " * 400
RESIDENCY_CONTAINMENT_FAULT_VALUE = "unreadable-cache-token-count"
RESIDENCY_CONTAINMENT_TOOL_ID = "containment-tool"
RESIDENCY_CONTAINMENT_TOOL_NAME = "Read"
RESIDENCY_CONTAINMENT_CONVERSATION_TEXT = "retained conversation"
RESIDENCY_CONTAINMENT_FAULT_SQL = "UPDATE spend_events SET cache_read_tok=? WHERE rowid=?"
RESIDENCY_CONTAINMENT_SCHEMA = """CREATE TABLE spend_events (
    source TEXT, conv_id TEXT, occurred_at TEXT, model TEXT,
    in_tok INTEGER, cache_read_tok INTEGER, cache_write_tok INTEGER,
    est_chat_usd TEXT)"""
RESIDENCY_CONTAINMENT_INSERT = "INSERT INTO spend_events VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
RESIDENCY_CONTAINMENT_REFERENCE = "SELECT rowid, model, cache_read_tok FROM spend_events ORDER BY rowid"


def exercise_residency_ledger_containment():
    """Prove containment preserves every row and makes a corrupt unit visible."""
    database = Path(os.environ["SPENDGUARD_HOME"]) / RESIDENCY_CONTAINMENT_DB_NAME
    seed_count = ledger_read.DEFAULT_LEDGER_READ_ROW_CAP + RESIDENCY_CONTAINMENT_EXTRA_ROWS
    with sqlite3.connect(database) as connection:
        connection.execute(RESIDENCY_CONTAINMENT_SCHEMA)
        connection.executemany(RESIDENCY_CONTAINMENT_INSERT, (
            (RESIDENCY_CONTAINMENT_SOURCE, RESIDENCY_CONTAINMENT_SESSION, str(index),
             RESIDENCY_CONTAINMENT_MODELS[index % len(RESIDENCY_CONTAINMENT_MODELS)],
             index, index + 1, 0, "0") for index in range(seed_count)))
        reference_rows = connection.execute(RESIDENCY_CONTAINMENT_REFERENCE).fetchall()
    reference = sum(pricing.realtime_cost(model, tokens, 0, cached_in_tok=tokens)
                    for _rowid, model, tokens in reference_rows)
    records = [
        {"type": "assistant", "message": {"model": RESIDENCY_CONTAINMENT_MODELS[0],
         "usage": {"input_tokens": 1}, "content": [
             {"type": "tool_use", "id": RESIDENCY_CONTAINMENT_TOOL_ID,
              "name": RESIDENCY_CONTAINMENT_TOOL_NAME}]}},
        {"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": RESIDENCY_CONTAINMENT_TOOL_ID,
             "content": RESIDENCY_CONTAINMENT_TOOL_TEXT},
            {"type": "text", "text": RESIDENCY_CONTAINMENT_CONVERSATION_TEXT}]}},
        {"type": "assistant", "message": {"model": RESIDENCY_CONTAINMENT_MODELS[0],
         "usage": {"input_tokens": 1}, "content": [
             {"type": "text", "text": RESIDENCY_CONTAINMENT_CONVERSATION_TEXT}]}},
    ]
    with patch.object(config, "db_path", return_value=str(database)), patch.object(
            claudecode, "_conv_transcript_records", return_value=records):
        totals = ledger_read.read_cc_residency_totals(RESIDENCY_CONTAINMENT_SESSION)
        assert totals["turns"] == seed_count
        assert totals["gaps"] == []
        assert math.isclose(totals["measured_recurring_usd"], reference)
        decomposition = claudecode.residency_decomposition(
            RESIDENCY_CONTAINMENT_SESSION, instruction_tokens=RESIDENCY_CONTAINMENT_INSTRUCTIONS)
        assert decomposition["turns"] == seed_count
        assert math.isclose(decomposition["measured_recurring_usd"], reference,
                            abs_tol=RESIDENCY_CONTAINMENT_PRECISION)
        assert math.isclose(sum(decomposition["by_source_usd"].values()), reference,
                            abs_tol=RESIDENCY_CONTAINMENT_PRECISION)
        assert all(value > 0 for value in decomposition["by_source_usd"].values())
        assert math.isclose(decomposition["per_turn_usd"], reference / seed_count,
                            abs_tol=RESIDENCY_CONTAINMENT_PRECISION)
        assert decomposition["top_contributors"]
        assert all(item["subagent_usd"] is not None for item in decomposition["top_contributors"])
        rendered = "\n".join(claudecode.refresh_residency_summary_cache(RESIDENCY_CONTAINMENT_SESSION))
        assert "unavailable" not in rendered, rendered
        assert "instructions" in rendered and "tool_result" in rendered and "conversation" in rendered
        assert "subagent" in rendered, rendered
        assert "/turn" in rendered, rendered

        # CHAOS: a single corrupt row AFTER the old cap must be named, not silently dropped.
        fault_rowid, fault_model, fault_tokens = reference_rows[-1]
        with sqlite3.connect(database) as connection:
            connection.execute(RESIDENCY_CONTAINMENT_FAULT_SQL,
                               (RESIDENCY_CONTAINMENT_FAULT_VALUE, fault_rowid))
        damaged = ledger_read.read_cc_residency_totals(RESIDENCY_CONTAINMENT_SESSION)
        assert damaged["turns"] == seed_count
        assert len(damaged["gaps"]) == 1
        named_gap = damaged["gaps"][0]
        assert named_gap["unit"] == f"spend_events:{fault_rowid}"
        assert RESIDENCY_CONTAINMENT_FAULT_VALUE in named_gap["error"]
        assert "Traceback" in named_gap["traceback"]
        readable_reference = reference - pricing.realtime_cost(
            fault_model, fault_tokens, 0, cached_in_tok=fault_tokens)
        assert math.isclose(damaged["measured_recurring_usd"], readable_reference)
        damaged_lines = "\n".join(claudecode.refresh_residency_summary_cache(RESIDENCY_CONTAINMENT_SESSION))
        assert "GAP" in damaged_lines and named_gap["unit"] in damaged_lines, damaged_lines
    print(f"OK residency_ledger_containment: all {seed_count} rows priced; corrupt tail named")


exercise_residency_ledger_containment()
