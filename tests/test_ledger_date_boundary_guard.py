"""Date-granular budget windows must reject timestamps instead of hiding spend."""
import datetime
from decimal import Decimal
from functools import partial
from unittest.mock import patch

from _pytest_fixture_bridge import align_test_startup_home

align_test_startup_home(prefix=__name__)

from spendguard import budget, ledger

DATE_BOUNDARY_DAY = "2026-10-30"
DATE_BOUNDARY_TIMESTAMP = DATE_BOUNDARY_DAY + "T14:22:05+00:00"
DATE_BOUNDARY_COST = Decimal("1.25")
DATE_BOUNDARY_SOURCE = "date-boundary-regression"
DATE_BOUNDARY_INVALID_VALUES = (
    DATE_BOUNDARY_TIMESTAMP,
    DATE_BOUNDARY_DAY + " 14:22:05",
    DATE_BOUNDARY_DAY + "+00:00",
    "2026-02-30",
    "20261030",
    "2026-W44-5",
    "2026-1-1",
    datetime.date.fromisoformat(DATE_BOUNDARY_DAY),
    datetime.datetime.fromisoformat(DATE_BOUNDARY_TIMESTAMP),
    False,
)


def test_ledger_calendar_boundaries_preserve_spend_and_fail_loud():
    boundary_ledger = budget._ledger()
    boundary_ledger.record_event({
        "source": DATE_BOUNDARY_SOURCE, "day": DATE_BOUNDARY_DAY,
        "realtime_usd": str(DATE_BOUNDARY_COST),
    })
    for empty_or_date_bound in (DATE_BOUNDARY_DAY, None, ""):
        assert budget.spent_since(empty_or_date_bound) == float(DATE_BOUNDARY_COST)
        assert budget.spent_since(empty_or_date_bound, read_only=True) == float(DATE_BOUNDARY_COST)
        assert Decimal(boundary_ledger.spent_dec(since=empty_or_date_bound)) == DATE_BOUNDARY_COST
    assert len(boundary_ledger.query(since=DATE_BOUNDARY_DAY, until=DATE_BOUNDARY_DAY)) == 1
    assert len(boundary_ledger.in_category("realtime", since=DATE_BOUNDARY_DAY)) == 1

    for boundary_name in ("since", "until"):
        for invalid_boundary in DATE_BOUNDARY_INVALID_VALUES:
            invalid_options = {boundary_name: invalid_boundary}
            boundary_operations = [
                partial(boundary_ledger._where, invalid_options.get("since"),
                        invalid_options.get("until"), None),
                partial(boundary_ledger.query, **invalid_options),
                partial(boundary_ledger.in_category, "realtime", **invalid_options),
                partial(boundary_ledger.rollup, **invalid_options),
                partial(boundary_ledger.spent_dec, **invalid_options),
            ]
            if boundary_name == "since":
                boundary_operations.extend((
                    partial(budget.spent_since, invalid_boundary),
                    partial(budget.spent_since, invalid_boundary, read_only=True),
                ))
            for boundary_operation in boundary_operations:
                try:
                    boundary_operation()
                except ledger.LedgerDateBoundaryError as boundary_error:
                    assert str(boundary_error) == (
                        f"{boundary_name} must be a YYYY-MM-DD date — the ledger 'day' column is date-granular; "
                        f"got {invalid_boundary!r}. Pass a date (truncate the timestamp), "
                        "or read the timestamp-granular calls table directly.")
                else:
                    raise AssertionError(f"date boundary silently accepted: {invalid_options!r}")

    # Document the actual dangerous result, using the real SQL and seeded row.
    with patch.object(ledger, "validate_ledger_date_bounds", return_value=None):
        assert budget.spent_since(DATE_BOUNDARY_TIMESTAMP) == 0
        assert boundary_ledger.query(since=DATE_BOUNDARY_TIMESTAMP) == []


if __name__ == "__main__":
    test_ledger_calendar_boundaries_preserve_spend_and_fail_loud()
    print("ledger calendar boundary guard passed")
