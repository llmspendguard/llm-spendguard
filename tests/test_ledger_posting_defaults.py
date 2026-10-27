"""Posting snapshots avoid per-row local-clock conversion without changing accounting."""
import datetime
from decimal import Decimal
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import MappingProxyType
from unittest.mock import patch

from _pytest_fixture_bridge import align_test_startup_home

align_test_startup_home(prefix=__name__)

from spendguard import budget, ledger, ledger_sync, model_catalog  # noqa: E402

POSTING_GUARD_ID = Path(__file__).stem
POSTING_GUARD_OPERATIONS = tuple(range(3))
POSTING_GUARD_ROWS = tuple(range(4))
POSTING_GUARD_FAULT_AT = POSTING_GUARD_ROWS[len(POSTING_GUARD_ROWS) // 2]
POSTING_GUARD_AMOUNT = len(POSTING_GUARD_ROWS)
POSTING_GUARD_MODEL = next(iter(model_catalog.as_price_table()))
POSTING_GUARD_DAY = (datetime.datetime.now(datetime.timezone.utc)
                     - datetime.timedelta(days=len(POSTING_GUARD_ROWS))).date().isoformat()
POSTING_GUARD_DATETIME = datetime.datetime
POSTING_GUARD_SNAPSHOT_INSTANT = datetime.datetime.now(datetime.timezone.utc)


class PostingClockFault(BaseException):
    """Injected mid-operation interruption must retain already committed evidence."""


class PostingLocalConversionForbidden(POSTING_GUARD_DATETIME):
    def astimezone(self, tz=None):
        raise PostingClockFault("eager source-local conversion")



posting_guard_original_record = ledger.SpendLedger.record_event
posting_guard_fault_calls = []


def interrupt_posting_midstep(self, event, venv=None):
    if len(posting_guard_fault_calls) == POSTING_GUARD_FAULT_AT:
        raise PostingClockFault("interrupted posting")
    posting_guard_fault_calls.append(event["dedup_key"])
    return posting_guard_original_record(self, event, venv=venv)


posting_guard_metadata = ledger.ledger_posting_metadata()
try:
    posting_guard_metadata["recorded_at"] = None
except TypeError:
    pass
else:
    raise AssertionError("posting metadata is mutable")
with patch.object(ledger.datetime, "datetime", PostingLocalConversionForbidden):
    with patch.object(ledger.SpendLedger, "record_event", interrupt_posting_midstep):
        try:
            for posting_guard_row in POSTING_GUARD_ROWS:
                budget.record_true_down(POSTING_GUARD_DAY, POSTING_GUARD_ID, POSTING_GUARD_MODEL,
                    POSTING_GUARD_AMOUNT, f"{POSTING_GUARD_ID}-fault-{posting_guard_row}",
                    posting_metadata=posting_guard_metadata)
        except PostingClockFault as posting_guard_error:
            assert str(posting_guard_error) == "interrupted posting"
        else:
            raise AssertionError("fault did not escape")
    assert len(list(budget._ledger()._conn.execute("SELECT * FROM spend_events WHERE provider=?", (POSTING_GUARD_ID,)))) == POSTING_GUARD_FAULT_AT

# Capture each independent snapshot outside the clock fault, then prohibit per-row conversions.
posting_guard_snapshots = {
    op: MappingProxyType({**ledger.ledger_posting_metadata(), "recorded_at": (
        POSTING_GUARD_SNAPSHOT_INSTANT - datetime.timedelta(seconds=op)
    ).isoformat(timespec="seconds")}) for op in POSTING_GUARD_OPERATIONS
}
assert len({value["recorded_at"] for value in posting_guard_snapshots.values()}) == len(POSTING_GUARD_OPERATIONS)
assert len({id(value) for value in posting_guard_snapshots.values()}) == len(POSTING_GUARD_OPERATIONS)
posting_guard_expected_metadata = {
    f"{POSTING_GUARD_ID}-{op}-{row}": posting_guard_snapshots[op]
    for op in POSTING_GUARD_OPERATIONS for row in POSTING_GUARD_ROWS
}
posting_guard_expected_metadata.update({
    f"{POSTING_GUARD_ID}-fault-{row}": posting_guard_metadata for row in POSTING_GUARD_ROWS
})


def write_posting_snapshot_operation(operation):
    metadata = posting_guard_snapshots[operation]
    for row_index in POSTING_GUARD_ROWS:
        budget.record_true_down(POSTING_GUARD_DAY, POSTING_GUARD_ID, POSTING_GUARD_MODEL,
            POSTING_GUARD_AMOUNT, f"{POSTING_GUARD_ID}-{operation}-{row_index}", posting_metadata=metadata)
    return operation


with patch.object(ledger.datetime, "datetime", PostingLocalConversionForbidden):
    with ThreadPoolExecutor(max_workers=len(POSTING_GUARD_OPERATIONS)) as posting_guard_pool:
        assert set(posting_guard_pool.map(write_posting_snapshot_operation, POSTING_GUARD_OPERATIONS)) == set(
            POSTING_GUARD_OPERATIONS)
posting_guard_rows = list(budget._ledger()._conn.execute("SELECT * FROM spend_events WHERE provider=?", (POSTING_GUARD_ID,)))
assert len(posting_guard_rows) == (len(POSTING_GUARD_ROWS) * len(POSTING_GUARD_OPERATIONS)
                                 + POSTING_GUARD_FAULT_AT)
assert len({row["id"] for row in posting_guard_rows}) == len(posting_guard_rows)
for posting_guard_event in posting_guard_rows:
    assert posting_guard_event["day"] == POSTING_GUARD_DAY
    assert posting_guard_event["occurred_at"].startswith(POSTING_GUARD_DAY)
    assert Decimal(posting_guard_event["batch_usd"]) == -POSTING_GUARD_AMOUNT
    for posting_guard_field in posting_guard_metadata:
        assert posting_guard_event[posting_guard_field] == posting_guard_expected_metadata[
            posting_guard_event["project_primary"]][posting_guard_field]
assert budget._ledger().verify_audit_chain(detail=True)["ok"]
# Exercise the real staging/clear/post path, not only callers supplying metadata.
posting_guard_integration_provider = POSTING_GUARD_ID + "-integration"
for posting_guard_index in POSTING_GUARD_ROWS:
    budget._record_spend_event(posting_guard_integration_provider, POSTING_GUARD_MODEL, "batch",
        POSTING_GUARD_AMOUNT, project=f"{POSTING_GUARD_ID}-integration-{posting_guard_index}",
        occurred_at=POSTING_GUARD_DAY + "T00:00:00+00:00", source=POSTING_GUARD_ID)
posting_guard_integration_snapshot = ledger.ledger_posting_metadata()
posting_guard_billed_divisor = len(POSTING_GUARD_OPERATIONS) - 1
posting_guard_billed = {posting_guard_integration_provider: [(
    posting_guard_integration_provider, POSTING_GUARD_MODEL,
    POSTING_GUARD_AMOUNT * len(POSTING_GUARD_ROWS) / posting_guard_billed_divisor,
    0, 0, POSTING_GUARD_DAY, POSTING_GUARD_ID)]}
with patch.object(ledger, "ledger_posting_metadata", return_value=posting_guard_integration_snapshot) as posting_capture:
    with patch.object(ledger.datetime, "datetime", PostingLocalConversionForbidden):
        posting_guard_summary = ledger_sync.true_down(POSTING_GUARD_DAY, posting_guard_billed)
posting_capture.assert_called_once_with()
assert not posting_guard_summary["read_gaps"]
posting_guard_corrections = list(budget._ledger()._conn.execute(
    "SELECT * FROM spend_events WHERE provider=? AND conv_id=?",
    (posting_guard_integration_provider, budget._TRUE_DOWN_CONV)))
assert len(posting_guard_corrections) == len(POSTING_GUARD_ROWS)
assert len({event["id"] for event in posting_guard_corrections}) == len(POSTING_GUARD_ROWS)
for posting_guard_event in posting_guard_corrections:
    assert posting_guard_event["day"] == POSTING_GUARD_DAY
    assert Decimal(posting_guard_event["batch_usd"]) == -Decimal(POSTING_GUARD_AMOUNT) / posting_guard_billed_divisor
    for posting_guard_field, posting_guard_value in posting_guard_integration_snapshot.items():
        assert posting_guard_event[posting_guard_field] == posting_guard_value
assert budget._ledger().verify_audit_chain(detail=True)["ok"]
# Explicit None remains supplied; asymmetric absence still requests a local default.
posting_guard_explicit_none = {
    "source": POSTING_GUARD_ID, "dedup_key": POSTING_GUARD_ID + "-none",
    "kind": "batch", "usd": POSTING_GUARD_AMOUNT,
    "ts_utc": POSTING_GUARD_DAY + "T00:00:00+00:00", "recorded_at": None,
    "tz": None, "local_datetime": None,
}
with patch.object(ledger.datetime, "datetime", PostingLocalConversionForbidden):
    posting_guard_none_id = budget._ledger().record_event(posting_guard_explicit_none)
    for posting_guard_missing_key in ("tz", "local_datetime"):
        posting_guard_missing_event = dict(posting_guard_explicit_none)
        del posting_guard_missing_event[posting_guard_missing_key]
        posting_guard_missing_event["dedup_key"] += posting_guard_missing_key
        try:
            budget._ledger().record_event(posting_guard_missing_event)
        except PostingClockFault as posting_guard_error:
            assert str(posting_guard_error) == "eager source-local conversion"
        else:
            raise AssertionError("missing local field did not request its default")
posting_guard_none_row = budget._ledger()._conn.execute(
    "SELECT recorded_at,tz,local_datetime FROM spend_events WHERE id=?", (posting_guard_none_id,)).fetchone()
assert tuple(posting_guard_none_row) == (None, None, None)
assert budget._ledger()._conn.execute(
    "SELECT COUNT(*) FROM spend_audit WHERE pass=?", ("ingest",)).fetchone()[0] == budget._ledger().count_events()
assert budget._ledger().verify_audit_chain(detail=True)["ok"]
print("OK posting defaults: complete concurrent rows, immutable snapshots, midstep interruption, exact audit")
