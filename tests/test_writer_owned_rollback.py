"""Caught SQL failures must abort the actual transaction owner, under a watchdog."""
import sys
from contextlib import nullcontext
from unittest.mock import patch

import pytest
from _pytest_fixture_bridge import align_test_startup_home, run_pytest_style

align_test_startup_home(prefix=__name__)

from spendguard import best_value, config, ledger_writer
from spendguard.ledger_read import ledger_request_scope

WRITER_ROLLBACK_BUDGET_S = 10
WRITER_ROLLBACK_OLD_FP = "old"
WRITER_ROLLBACK_NEW_FP = "new"
WRITER_ROLLBACK_TRIGGER = """CREATE TRIGGER reject_verdict_delete BEFORE DELETE ON bestvalue_verdict
BEGIN SELECT RAISE(FAIL, 'injected retention failure'); END"""
WRITER_ROLLBACK_VERDICT = {"model": None, "considered": {}, "why_base": __name__}


@pytest.mark.parametrize("writer_owned", [False, True])
@pytest.mark.parametrize("reset_owner", [False, True])
def test_caught_second_statement_rolls_back_actual_owner(writer_owned, reset_owner):
    intent = test_caught_second_statement_rolls_back_actual_owner.__name__
    key = best_value._VERDICT_CACHE_POOL_KEY
    def exercise_verdict_retention_failure():
        connection = best_value._bestvalue_verdict_db()
        connection.execute("DROP TRIGGER IF EXISTS reject_verdict_delete")
        connection.execute("DELETE FROM bestvalue_verdict")
        connection.commit()
        best_value._store_bestvalue_verdict.__wrapped__(intent, None, WRITER_ROLLBACK_OLD_FP,
                                                     WRITER_ROLLBACK_VERDICT)
        connection.execute(WRITER_ROLLBACK_TRIGGER)
        connection.commit()
        rollback = config.reset_ledger_conn if reset_owner else config.rollback_ledger_conn
        with patch.object(config, "rollback_ledger_conn", side_effect=rollback):
            best_value._store_bestvalue_verdict.__wrapped__(intent, None, WRITER_ROLLBACK_NEW_FP,
                                                         WRITER_ROLLBACK_VERDICT)
    with ledger_request_scope(deadline_s=WRITER_ROLLBACK_BUDGET_S):
        if writer_owned:
            ledger_writer.run_ledger_write(exercise_verdict_retention_failure)
        else:
            exercise_verdict_retention_failure()
    with config.ledger_op(key, best_value._ensure_bestvalue_verdict_schema) as reader:
        assert reader.execute("SELECT evidence_fp FROM bestvalue_verdict").fetchall() == [(WRITER_ROLLBACK_OLD_FP,)]
        reader.execute("DROP TRIGGER reject_verdict_delete")
    if writer_owned:
        ledger_writer.run_ledger_write(lambda: best_value._store_bestvalue_verdict.__wrapped__(
            intent, None, WRITER_ROLLBACK_NEW_FP, WRITER_ROLLBACK_VERDICT))
        with config.ledger_op(key, best_value._ensure_bestvalue_verdict_schema) as reader:
            assert reader.execute("SELECT evidence_fp FROM bestvalue_verdict").fetchall() == [(WRITER_ROLLBACK_NEW_FP,)]


@pytest.mark.parametrize("raise_after_abort", [False, True])
@pytest.mark.parametrize("bulk_scope", [False, True])
def test_writer_abort_has_one_rollback_and_no_commit(raise_after_abort, bulk_scope):
    rollback_handles = []
    commit_handles = []
    native_rollback = config._CountedLedgerConnection.rollback
    native_commit = config._CountedLedgerConnection.commit

    def observe_writer_abort_rollback(connection):
        rollback_handles.append(connection)
        return native_rollback(connection)

    def observe_writer_abort_commit(connection):
        commit_handles.append(connection)
        return native_commit(connection)

    def catch_writer_statement_and_abort():
        connection = best_value._bestvalue_verdict_db()
        connection.execute("INSERT OR REPLACE INTO bestvalue_verdict VALUES(?,?,?,?,?)",
                           (__name__, __name__, __name__, __name__, __name__))
        config.rollback_ledger_conn(best_value._VERDICT_CACHE_POOL_KEY)
        if raise_after_abort:
            raise RuntimeError(__name__)

    ledger_writer.run_ledger_write(best_value._bestvalue_verdict_db)
    with ledger_request_scope(deadline_s=WRITER_ROLLBACK_BUDGET_S), \
            patch.object(config._CountedLedgerConnection, "rollback", observe_writer_abort_rollback), \
            patch.object(config._CountedLedgerConnection, "commit", observe_writer_abort_commit):
        scope = ledger_writer.ledger_writer_bulk_transaction if bulk_scope else nullcontext
        if raise_after_abort:
            with pytest.raises(RuntimeError), scope():
                ledger_writer.run_ledger_write(catch_writer_statement_and_abort)
        else:
            with scope():
                ledger_writer.run_ledger_write(catch_writer_statement_and_abort)
    assert len(rollback_handles) == 1
    assert not commit_handles


if __name__ == "__main__":
    sys.exit(run_pytest_style(globals()))
