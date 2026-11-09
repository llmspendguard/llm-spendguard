"""A persisted pre-pure-SQL trigger is replaced before the first production write."""
import sqlite3

from spendguard import config, ledger
from spendguard.store.money_ledger import ledger_daily_rollup


STALE_TRIGGER_NAME = "fixture_stale_rollup_trigger"
STALE_FUNCTION_NAME = "fixture_removed_rollup_function"
EVENT_SOURCE = "rollup-trigger-convergence-fixture"
EVENT_DAY = "fixture-day"
EVENT_AMOUNT = "0.01"


def test_production_open_converges_stale_trigger_and_revision_makes_reopen_cheap(tmp_path):
    database_path = tmp_path / "rollup-trigger-convergence.sqlite"
    initial = ledger.SpendLedger(database_path, owned_connection=config.open_ledger_connection(database_path))
    initial._conn.execute(f"DELETE FROM {ledger_daily_rollup.ROLLUP_TRIGGER_SCHEMA_TABLE}")
    initial._conn.execute(
        f"CREATE TRIGGER {STALE_TRIGGER_NAME} AFTER INSERT ON spend_events "
        f"BEGIN SELECT {STALE_FUNCTION_NAME}(); END")
    initial._conn.commit()
    initial._conn.close()

    raw = sqlite3.connect(database_path)
    try:
        raw.execute("INSERT INTO spend_events(id,source,day) VALUES(?,?,?)",
                    ("raw-defect-witness", EVENT_SOURCE, EVENT_DAY))
    except sqlite3.OperationalError as defect_error:
        assert "no such function" in str(defect_error)
    else:
        raise AssertionError("injected stale trigger did not reproduce the pre-convergence write failure")
    raw.rollback()
    raw.close()

    connection = config.open_ledger_connection(database_path)
    statements = []
    connection.set_trace_callback(statements.append)
    healed = ledger.SpendLedger(database_path, owned_connection=connection)
    healed.record_event({"source": EVENT_SOURCE, "day": EVENT_DAY, "realtime_usd": EVENT_AMOUNT})
    assert healed._conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='trigger' AND name=?", (STALE_TRIGGER_NAME,)).fetchone() is None
    assert any("CREATE TRIGGER" in statement.upper() for statement in statements)
    healed._conn.close()

    reopened_connection = config.open_ledger_connection(database_path)
    reopened_statements = []
    reopened_connection.set_trace_callback(reopened_statements.append)
    reopened = ledger.SpendLedger(database_path, owned_connection=reopened_connection)
    assert not any("CREATE TRIGGER" in statement.upper() or "DROP TRIGGER" in statement.upper()
                   for statement in reopened_statements)
    reopened._conn.close()


if __name__ == "__main__":
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as directory:
        test_production_open_converges_stale_trigger_and_revision_makes_reopen_cheap(Path(directory))
    print("[OK] stale rollup triggers converge once before production writes")
