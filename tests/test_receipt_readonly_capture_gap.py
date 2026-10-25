"""Receipt reads a readonly ledger without tuning it and names every unavailable capture axis."""
import datetime
import os
import sqlite3
import stat
import sys
import tempfile

READONLY_FILE_MODE = stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH
WRITABLE_FILE_MODE = stat.S_IRUSR | stat.S_IWUSR
FIXTURE_COST = 0.25
SCHEMA_TABLE_REMOVED_FOR_MIGRATION = "spend_audit"

if not os.environ.get("SPENDGUARD_TEST_ISOLATED"):
    os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="sg-receipt-readonly-")
    os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"
    os.execv(sys.executable, [sys.executable] + sys.argv)
os.environ.setdefault("SPENDGUARD_NO_AUTOINSTALL", "1")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from spendguard import budget, config, receipt  # noqa: E402

budget.record_charge("openai", "fixture-model", "realtime", FIXTURE_COST)
budget._LEDGER_TL.led._conn.execute(f"DROP TABLE {SCHEMA_TABLE_REMOVED_FOR_MIGRATION}")
budget._LEDGER_TL.led._conn.commit()
budget._LEDGER_TL.led._conn.close()
budget._LEDGER_TL.led = None
ledger_path = config.db_path()
os.chmod(ledger_path, READONLY_FILE_MODE)
try:
    since = datetime.date.today().isoformat()
    try:
        budget.spent_since(since)
    except sqlite3.OperationalError as readonly_error:
        assert "readonly" in str(readonly_error).lower()
    else:
        raise AssertionError("write-capable ledger aggregate did not attempt readonly schema initialization")

    assert budget.spent_since(since, read_only=True) == FIXTURE_COST
    rendered = receipt.render_tree()
    assert "spend this month" in rendered
    assert "CAPTURE GAP: ledger read unavailable" not in rendered
finally:
    os.chmod(ledger_path, WRITABLE_FILE_MODE)

print("OK test_receipt_readonly_capture_gap: readonly receipt rendered from a pure-read handle")
