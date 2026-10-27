"""A caught nested failure must not commit partial event/audit or DDL work."""
import json
import sys
from unittest.mock import patch

import pytest
from _pytest_fixture_bridge import align_test_startup_home, run_pytest_style

align_test_startup_home(prefix=__name__)

from spendguard import config, ledger_deadletter, ledger_writer
from spendguard.ledger import SpendLedger
from spendguard.ledger_read import ledger_request_scope

NESTED_ISOLATION_DEADLINE_S = 10
NESTED_ISOLATION_COST = "0.017"
NESTED_ISOLATION_BAD_KEY = __name__ + "-failed"
NESTED_ISOLATION_GOOD_KEY = __name__ + "-sibling"


def test_replayed_audit_failure_retains_retry_without_partial_rows(tmp_path):
    current = SpendLedger()
    retry_path = tmp_path / (__name__ + ".jsonl")
    events = [{"dedup_key": key, "realtime_usd": NESTED_ISOLATION_COST}
              for key in (NESTED_ISOLATION_BAD_KEY, NESTED_ISOLATION_GOOD_KEY)]
    retry_path.write_text("".join(json.dumps({"event": event}) + "\n" for event in events))
    bad_id, good_id = [current._evidence_id(event) for event in events]
    original_audit = SpendLedger._audit

    def reject_only_replayed_audit(self, event_id, *args, **kwargs):
        if event_id == bad_id:
            raise OSError(__name__)
        return original_audit(self, event_id, *args, **kwargs)

    with ledger_request_scope(deadline_s=NESTED_ISOLATION_DEADLINE_S), \
            patch.object(SpendLedger, "_audit", reject_only_replayed_audit):
        report = ledger_deadletter.drain_spend_deadletter_events([retry_path])
    assert report["committed"] == report["retained"] == 1, report
    retained = json.loads(retry_path.read_text())
    assert retained["event"] == events[0] and retained["retry_attempts"] == 1

    def read_replay_atomicity_rows():
        connection = ledger_writer.current_ledger_writer_connection()
        return [(connection.execute("SELECT count(*) FROM spend_events WHERE id=?", (eid,)).fetchone()[0],
                 connection.execute("SELECT count(*) FROM spend_audit WHERE event_id=?", (eid,)).fetchone()[0])
                for eid in (bad_id, good_id)]

    assert ledger_writer.run_ledger_write(read_replay_atomicity_rows) == [(0, 0), (1, 1)]
    assert ledger_deadletter.drain_spend_deadletter_events([retry_path])["committed"] == 1
    assert retry_path.read_text() == ""
    assert ledger_writer.run_ledger_write(read_replay_atomicity_rows) == [(1, 1), (1, 1)]


def test_inline_bulk_record_failure_does_not_ride_sibling_commit():
    current = SpendLedger()
    event = {"dedup_key": __name__, "realtime_usd": NESTED_ISOLATION_COST}
    with ledger_request_scope(deadline_s=NESTED_ISOLATION_DEADLINE_S), current.bulk():
        with patch.object(SpendLedger, "_audit", side_effect=OSError(__name__)):
            with pytest.raises(OSError):
                current.record_event(event)
    assert ledger_writer.run_ledger_write(lambda: ledger_writer.current_ledger_writer_connection().execute(
        "SELECT count(*) FROM spend_events WHERE id=?", (current._evidence_id(event),)).fetchone()[0]) == 0


def test_caught_schema_touch_failure_in_bulk_rolls_back_ddl():
    current = SpendLedger()

    def fail_after_nested_schema_ddl(connection):
        connection.execute("CREATE TABLE nested_schema_failure_evidence (value TEXT)")
        raise OSError(__name__)

    with ledger_request_scope(deadline_s=NESTED_ISOLATION_DEADLINE_S), current.bulk():
        with pytest.raises(OSError):
            config.ensure_ledger_schema(ledger_writer.current_ledger_writer_connection(),
                                        fail_after_nested_schema_ddl)
    assert ledger_writer.run_ledger_write(lambda: ledger_writer.current_ledger_writer_connection().execute(
        "SELECT count(*) FROM sqlite_master WHERE name=?", ("nested_schema_failure_evidence",)).fetchone()[0]) == 0


@pytest.mark.parametrize("cleanup_statement", ["ROLLBACK TO SAVEPOINT", "RELEASE SAVEPOINT"])
def test_nested_cleanup_failure_aborts_owner_once(cleanup_statement):
    current = SpendLedger()
    event = {"dedup_key": cleanup_statement, "realtime_usd": NESTED_ISOLATION_COST}
    execute = ledger_writer.execute_ledger_savepoint_cleanup
    rollback = config._CountedLedgerConnection.rollback
    rollbacks = []

    def fail_nested_cleanup_sql(connection, statement, *args, **kwargs):
        if statement.startswith(cleanup_statement):
            raise OSError(__name__)
        return execute(connection, statement, *args, **kwargs)

    def observe_nested_cleanup_abort(connection):
        rollbacks.append(connection)
        return rollback(connection)

    def catch_cleanup_failure_inside_outer_writer():
        with patch.object(ledger_writer, "execute_ledger_savepoint_cleanup", fail_nested_cleanup_sql), \
                patch.object(config._CountedLedgerConnection, "rollback", observe_nested_cleanup_abort):
            with pytest.raises(OSError):
                if cleanup_statement == "ROLLBACK TO SAVEPOINT":
                    with patch.object(SpendLedger, "_audit", side_effect=OSError(__name__)):
                        current.record_event(event)
                else:
                    current.record_event(event)

    with ledger_request_scope(deadline_s=NESTED_ISOLATION_DEADLINE_S):
        ledger_writer.run_ledger_write(catch_cleanup_failure_inside_outer_writer)
    assert len(rollbacks) == 1
    assert ledger_writer.run_ledger_write(lambda: ledger_writer.current_ledger_writer_connection().execute(
        "SELECT count(*) FROM spend_events WHERE id=?", (current._evidence_id(event),)).fetchone()[0]) == 0


def test_nested_drain_refuses_before_touching_retry_evidence(tmp_path):
    current = SpendLedger()
    retry_path = tmp_path / (__name__ + "-nested.jsonl")
    original = json.dumps({"event": {"dedup_key": __name__ + "-nested"}}) + "\n"
    retry_path.write_text(original)
    with ledger_request_scope(deadline_s=NESTED_ISOLATION_DEADLINE_S), current.bulk():
        with pytest.raises(RuntimeError):
            ledger_deadletter.drain_spend_deadletter_events([retry_path])
    assert retry_path.read_text() == original


def test_cross_spool_commit_failure_preserves_successes_and_attempted_retries(tmp_path):
    current = SpendLedger()
    events = [{"dedup_key": __name__ + suffix, "realtime_usd": NESTED_ISOLATION_COST}
              for suffix in ("-commit-sibling", "-commit-failure")]
    retry_paths = [tmp_path / (event["dedup_key"] + ".jsonl") for event in events]
    originals = [json.dumps({"event": event}) + "\n" for event in events]
    for path, original in zip(retry_paths, originals):
        path.write_text(original)
    failing_id = current._evidence_id(events[-1])
    audit = SpendLedger._audit

    def reject_cross_spool_audit(self, event_id, *args, **kwargs):
        if event_id == failing_id:
            raise OSError(__name__)
        return audit(self, event_id, *args, **kwargs)

    with ledger_request_scope(deadline_s=NESTED_ISOLATION_DEADLINE_S), \
            patch.object(SpendLedger, "_audit", reject_cross_spool_audit), \
            patch.object(config._CountedLedgerConnection, "commit", side_effect=OSError(__name__)):
        with pytest.raises(OSError):
            ledger_deadletter.drain_spend_deadletter_events(retry_paths)
    assert retry_paths[0].read_text() == originals[0]
    assert json.loads(retry_paths[-1].read_text())["retry_attempts"] == 1
    for event in events:
        assert ledger_writer.run_ledger_write(lambda event=event: ledger_writer.current_ledger_writer_connection().execute(
            "SELECT count(*) FROM spend_events WHERE id=?", (current._evidence_id(event),)).fetchone()[0]) == 0


if __name__ == "__main__":
    sys.exit(run_pytest_style(globals()))
