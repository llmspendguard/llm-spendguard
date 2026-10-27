"""Deadline contention must release the writer and preserve unattempted evidence."""
import fcntl
import json
import sys
import threading
import time
from unittest.mock import patch
import pytest

from _pytest_fixture_bridge import align_test_startup_home, run_pytest_style

align_test_startup_home(prefix=__name__)

from spendguard import budget, config, ledger, ledger_deadletter, ledger_read, ledger_writer

DEADLETTER_EXPIRY_BUDGET_S = 0.05
DEADLETTER_EXPIRY_WATCHDOG_S = DEADLETTER_EXPIRY_BUDGET_S * 100
DEADLETTER_EXPIRY_WRITE_BUDGET_S = DEADLETTER_EXPIRY_BUDGET_S * 10
DEADLETTER_EXPIRY_TAIL_COUNT = 4
DEADLETTER_EXPIRY_EVENT_COST = "0.001"


def test_spool_lock_expiry_releases_writer_for_unrelated_spend(tmp_path):
    current = ledger.SpendLedger()
    retry_path = tmp_path / (__name__ + ".jsonl")
    evidence = json.dumps({"event": {}}) + "\n"
    retry_path.write_text(evidence)
    entered = threading.Event()
    failures = []
    original_lock = ledger_deadletter.locked_spend_deadletter_path

    def observe_expiry_spool_lock(path):
        entered.set()
        return original_lock(path)

    def dispatch_expiring_spool_drain():
        try:
            with ledger_read.ledger_request_scope(deadline_s=DEADLETTER_EXPIRY_BUDGET_S):
                ledger_deadletter.drain_spend_deadletter_events([retry_path])
        except ledger_read.LedgerQueryDeadline:
            pass
        except BaseException as exc:
            failures.append(exc)

    with open(str(retry_path) + ledger_deadletter.LEDGER_DEADLETTER_LOCK_SUFFIX, "a") as blocker:
        fcntl.flock(blocker, fcntl.LOCK_EX)
        worker = threading.Thread(target=dispatch_expiring_spool_drain)
        try:
            with patch.object(ledger_deadletter, "locked_spend_deadletter_path", observe_expiry_spool_lock):
                worker.start()
                assert entered.wait(DEADLETTER_EXPIRY_WATCHDOG_S)
                worker.join(DEADLETTER_EXPIRY_WRITE_BUDGET_S)
                assert not worker.is_alive()
                with ledger_read.ledger_request_scope(deadline_s=DEADLETTER_EXPIRY_WRITE_BUDGET_S):
                    current.record_event({"dedup_key": __name__, "realtime_usd": DEADLETTER_EXPIRY_EVENT_COST})
                assert retry_path.read_text() == evidence
        finally:
            fcntl.flock(blocker, fcntl.LOCK_UN)
            worker.join(DEADLETTER_EXPIRY_WATCHDOG_S)
    assert not worker.is_alive() and not failures, failures


def test_deadline_tail_keeps_original_bytes_and_retries(tmp_path):
    ledger.SpendLedger()
    retry_path = tmp_path / (__name__ + ".jsonl")
    first = json.dumps({"event": {}, "retry_attempts": 0}) + "\n"
    tail = [json.dumps({"event": {"dedup_key": str(index)}, "retry_attempts": 0})
            + "  \r\n" for index in range(DEADLETTER_EXPIRY_TAIL_COUNT)]
    retry_path.write_bytes((first + "".join(tail)).encode())
    attempted = []

    def fail_attempt_then_exhaust_deadline(self, event, **kwargs):
        attempted.append(event)
        scope = ledger_read.current_ledger_request_scope()
        while time.monotonic() < scope.deadline_at:
            time.sleep(max(0, scope.deadline_at - time.monotonic()))
        raise OSError(__name__)

    with patch.object(ledger.SpendLedger, "record_event", fail_attempt_then_exhaust_deadline):
        try:
            with ledger_read.ledger_request_scope(deadline_s=DEADLETTER_EXPIRY_BUDGET_S):
                ledger_deadletter.drain_spend_deadletter_events([retry_path])
        except ledger_read.LedgerQueryDeadline:
            pass
    ledger_writer.run_ledger_write(lambda: None)
    remaining = retry_path.read_bytes().decode().splitlines(keepends=True)
    assert len(attempted) == 1, attempted
    assert json.loads(remaining[0])["retry_attempts"] == 1
    assert remaining[1:] == tail, remaining
    with patch.object(ledger.SpendLedger, "record_event", return_value=None):
        assert ledger_deadletter.drain_spend_deadletter_events([retry_path])["committed"] == len(remaining)
    assert retry_path.read_text() == ""


def test_expired_primary_charge_preserves_complete_spool_evidence(tmp_path):
    retry_path = tmp_path / (__name__ + "-recovery.jsonl")
    emergency_path = tmp_path / (__name__ + "-emergency.jsonl")
    with patch.object(budget, "spend_event_retry_paths", return_value=(retry_path, emergency_path)):
        with ledger_read.ledger_request_scope(deadline_s=DEADLETTER_EXPIRY_BUDGET_S) as expired_scope:
            expired_scope.deadline_at = time.monotonic()
            budget._record_spend_event(__name__, __name__, "realtime", DEADLETTER_EXPIRY_EVENT_COST)
            assert ledger_read.current_ledger_request_scope() is expired_scope
    evidence = json.loads(retry_path.read_text())
    assert evidence["cost"] == DEADLETTER_EXPIRY_EVENT_COST
    assert evidence["event"]["usd"] == DEADLETTER_EXPIRY_EVENT_COST
    assert evidence["event"]["billed"] == 1
    assert "LedgerQueryDeadline" in evidence["error"]
    assert not emergency_path.exists()


def test_expired_charge_uses_emergency_spool_when_home_append_fails(tmp_path):
    home_path = tmp_path / (__name__ + "-home-directory")
    home_path.mkdir()
    emergency_path = tmp_path / (__name__ + "-fallback.jsonl")
    with patch.object(budget, "spend_event_retry_paths", return_value=(home_path, emergency_path)):
        with ledger_read.ledger_request_scope(deadline_s=DEADLETTER_EXPIRY_BUDGET_S) as expired_scope:
            expired_scope.deadline_at = time.monotonic()
            budget._record_spend_event(__name__, __name__, "realtime", DEADLETTER_EXPIRY_EVENT_COST)
    assert json.loads(emergency_path.read_text())["event"]["usd"] == DEADLETTER_EXPIRY_EVENT_COST


def test_recovery_lock_uses_own_budget_and_surfaces_exhaustion(tmp_path):
    retry_path = tmp_path / (__name__ + "-blocked.jsonl")
    with open(str(retry_path) + ledger_deadletter.LEDGER_DEADLETTER_LOCK_SUFFIX, "a") as blocker:
        fcntl.flock(blocker, fcntl.LOCK_EX)
        with patch.object(config, "LEDGER_EVIDENCE_PERSISTENCE_BUDGET_S", DEADLETTER_EXPIRY_BUDGET_S):
            with ledger_read.ledger_request_scope(deadline_s=DEADLETTER_EXPIRY_BUDGET_S) as expired_scope:
                expired_scope.deadline_at = time.monotonic()
                started = time.monotonic()
                with pytest.raises(ledger_read.LedgerQueryDeadline):
                    ledger_deadletter.append_spend_deadletter_event(retry_path, {"event": {}})
                assert time.monotonic() - started >= DEADLETTER_EXPIRY_BUDGET_S
                assert time.monotonic() - started < DEADLETTER_EXPIRY_WATCHDOG_S
                assert ledger_read.current_ledger_request_scope() is expired_scope
        fcntl.flock(blocker, fcntl.LOCK_UN)
    assert not retry_path.exists()


if __name__ == "__main__":
    sys.exit(run_pytest_style(globals()))
