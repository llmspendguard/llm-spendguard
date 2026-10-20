"""Receipt display must retain complete residency totals beyond the ordinary read cap."""
import contextlib
import io
import math
import os
import sqlite3
import tempfile
from pathlib import Path
from unittest.mock import patch

import spendguard
from spendguard import claudecode, config, ledger_read, pricing, receipt

DISPLAY_RESIDENCY_EXTRA_ROWS = 1
DISPLAY_RESIDENCY_DATABASE_NAME = "receipt-residency.sqlite"
DISPLAY_RESIDENCY_SESSION = "display-containment-session"
DISPLAY_RESIDENCY_MONEY_DIGITS = 4
DISPLAY_RESIDENCY_TEXT = "resident conversation evidence"
DISPLAY_RESIDENCY_SCHEMA = """CREATE TABLE spend_events (
    source TEXT, conv_id TEXT, occurred_at TEXT, model TEXT, cache_read_tok INTEGER)"""
DISPLAY_RESIDENCY_INSERT = "INSERT INTO spend_events VALUES (?, ?, ?, ?, ?)"
DISPLAY_RESIDENCY_EXPECTED_LINE = "residency : Est sub value cache-read $%.4f"


def run_receipt_residency_display_containment():
    spendguard.require()
    with tempfile.TemporaryDirectory() as display_residency_home:
        display_residency_path = Path(display_residency_home) / DISPLAY_RESIDENCY_DATABASE_NAME
        display_residency_count = (
            ledger_read.DEFAULT_LEDGER_READ_ROW_CAP + DISPLAY_RESIDENCY_EXTRA_ROWS)
        display_residency_model = config.advisor_model()
        display_residency_cached_rate = pricing.price(display_residency_model)["cached_in"]
        assert display_residency_cached_rate > 0
        display_residency_seed = [
            (ledger_read.CC_RESIDENCY_SOURCE, DISPLAY_RESIDENCY_SESSION, str(index),
             display_residency_model, index)
            for index in range(display_residency_count)]
        with contextlib.closing(sqlite3.connect(display_residency_path)) as display_residency_writer:
            display_residency_writer.execute(DISPLAY_RESIDENCY_SCHEMA)
            display_residency_writer.executemany(DISPLAY_RESIDENCY_INSERT, display_residency_seed)
            display_residency_writer.commit()
        display_residency_snapshot = display_residency_path.read_bytes()
        display_residency_expected = sum(row[-1] for row in display_residency_seed)
        display_residency_expected *= (
            display_residency_cached_rate / ledger_read.CC_RESIDENCY_TOKEN_SCALE)
        display_residency_records = [{"type": "user", "message": {
            "role": "user", "content": DISPLAY_RESIDENCY_TEXT}}]
        display_residency_stdout = io.StringIO()
        with patch.dict(os.environ, {"SPENDGUARD_HOME": display_residency_home}), \
                patch.object(config, "HOME", Path(display_residency_home)), \
                patch.object(config, "db_path", return_value=display_residency_path), \
                patch.object(claudecode, "_conv_transcript_records", return_value=display_residency_records), \
                patch.object(claudecode, "_resident_instruction_tokens", return_value=(0, [])), \
                patch.object(receipt, "render_tree", return_value=""), \
                patch.object(receipt, "_conv_org", return_value=None), \
                patch.object(receipt, "_est_tree", return_value={}), \
                patch("spendguard.lane_value.refresh_lane_value_if_stale"), \
                contextlib.redirect_stdout(display_residency_stdout):
            display_residency_totals = ledger_read.read_cc_residency_totals()
            assert display_residency_totals["turns"] == display_residency_count
            assert math.isclose(display_residency_totals["measured_recurring_usd"], display_residency_expected)
            display_residency_decomposition = claudecode.residency_decomposition(DISPLAY_RESIDENCY_SESSION)
            assert display_residency_decomposition["reconciles"]
            assert display_residency_decomposition["measured_recurring_usd"] == round(
                display_residency_expected, DISPLAY_RESIDENCY_MONEY_DIGITS)
            assert sum(display_residency_decomposition["by_source_usd"].values()) == round(
                display_residency_expected, DISPLAY_RESIDENCY_MONEY_DIGITS)
            assert receipt.cli(["--all"]) == 0
        assert DISPLAY_RESIDENCY_EXPECTED_LINE % display_residency_expected in display_residency_stdout.getvalue()
        assert "unavailable" not in display_residency_stdout.getvalue()
        assert display_residency_path.read_bytes() == display_residency_snapshot
        print("Receipt residency display: complete totals beyond default cap; forensic bytes unchanged")


if __name__ == "__main__":
    run_receipt_residency_display_containment()
