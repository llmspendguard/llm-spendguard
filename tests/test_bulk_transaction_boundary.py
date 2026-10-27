"""A bulk scope owns its schema, pending reads, and final commit."""
import sys

import pytest
from _pytest_fixture_bridge import align_test_startup_home, run_pytest_style

align_test_startup_home(prefix=__name__)

from spendguard import ledger

BULK_BOUNDARY_EVENT = {"source": __name__, "kind": "realtime", "usd": 1,
                       "dedup_key": __name__}


def test_schema_touch_cannot_commit_failed_bulk():
    current = ledger.SpendLedger()
    before = current.sum_dec()
    with pytest.raises(RuntimeError):
        with current.bulk():
            event_id = current.record_event({**BULK_BOUNDARY_EVENT,
                "dedup_key": test_schema_touch_cannot_commit_failed_bulk.__name__})
            ledger.SpendLedger()
            raise RuntimeError(__name__)
    assert current.get(event_id) is None
    assert current.sum_dec() == before


def test_bulk_reserved_connection_reads_pending_event():
    current = ledger.SpendLedger()
    before = ledger.to_dec(current.sum_dec())
    with current.bulk():
        event_id = current.record_event({**BULK_BOUNDARY_EVENT,
            "dedup_key": test_bulk_reserved_connection_reads_pending_event.__name__})
        assert current.get(event_id) is not None
        assert current.history(event_id)
        assert ledger.to_dec(current.sum_dec()) == before + BULK_BOUNDARY_EVENT["usd"]
        assert current.spent_dec() == current.running_llm_total_dec()
        assert current.query(where={"id": event_id})
        assert current.rollup()["n"]


def test_worker_inline_bulk_keeps_reads_and_rollback_on_owner():
    from spendguard.ledger_writer import run_ledger_write

    current = ledger.SpendLedger()
    pending_ids = []

    def exercise_worker_owned_bulk_boundary():
        with current.bulk():
            event_id = current.record_event({**BULK_BOUNDARY_EVENT,
                "dedup_key": test_worker_inline_bulk_keeps_reads_and_rollback_on_owner.__name__})
            pending_ids.append(event_id)
            assert current.get(event_id) is not None
            assert current.query(where={"id": event_id})
            ledger.SpendLedger()
            raise RuntimeError(__name__)

    with pytest.raises(RuntimeError):
        run_ledger_write(exercise_worker_owned_bulk_boundary)
    assert current.get(pending_ids[0]) is None


def test_explicit_owned_bulk_reports_use_pending_transaction():
    from spendguard.ledger_writer import exclusive_ledger_writer_connection

    with exclusive_ledger_writer_connection() as connection:
        current = ledger.SpendLedger(owned_connection=connection)
        before = ledger.to_dec(current.sum_dec())
        with pytest.raises(RuntimeError):
            with current.bulk():
                event_id = current.record_event({**BULK_BOUNDARY_EVENT,
                    "dedup_key": test_explicit_owned_bulk_reports_use_pending_transaction.__name__})
                assert current.query(where={"id": event_id})
                assert ledger.to_dec(current.sum_dec()) == before + BULK_BOUNDARY_EVENT["usd"]
                raise RuntimeError(__name__)
        assert current.get(event_id) is None


if __name__ == "__main__":
    sys.exit(run_pytest_style(globals()))
