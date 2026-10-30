"""Receipt reads a readonly ledger without tuning it and names every unavailable capture axis."""
import datetime
import contextlib
import os
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
from spendguard.ledger_writer import close_ledger_writers

budget.record_charge("openai", "fixture-model", "realtime", FIXTURE_COST)
with contextlib.closing(config.open_ledger_connection(config.db_path())) as fixture_connection:
    fixture_connection.execute(f"DROP TABLE {SCHEMA_TABLE_REMOVED_FOR_MIGRATION}")
    fixture_connection.commit()
budget._LEDGER_TL.led = None
close_ledger_writers()
ledger_path = config.db_path()
os.chmod(ledger_path, READONLY_FILE_MODE)
try:
    since = datetime.date.today().isoformat()
    assert budget.spent_since(since) == FIXTURE_COST
    assert budget.spent_since(since, read_only=True) == FIXTURE_COST
    rendered = receipt.render_tree()
    assert "spend this month" in rendered
    assert "CAPTURE GAP: ledger read unavailable" not in rendered
finally:
    os.chmod(ledger_path, WRITABLE_FILE_MODE)

print("OK test_receipt_readonly_capture_gap: readonly receipt rendered from a pure-read handle")
