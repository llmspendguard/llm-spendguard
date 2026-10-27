"""Fresh local ledger timestamps use aware instants across timezone transitions."""
import datetime
import os
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

from spendguard import ledger

LOCAL_TIMESTAMP_CLOCK_ZONES = ("UTC0", "EST5EDT,M3.2.0,M11.1.0")
LOCAL_TIMESTAMP_CLOCK_INSTANTS = (
    datetime.datetime(2026, 11, 1, 5, 30, tzinfo=datetime.timezone.utc),
    datetime.datetime(2026, 11, 1, 6, 30, tzinfo=datetime.timezone.utc),
)
LOCAL_TIMESTAMP_CLOCK_AMOUNT = "1"
LOCAL_TIMESTAMP_CLOCK_DATABASE = Path(__file__).stem + ".sqlite"
LOCAL_TIMESTAMP_CLOCK_SOURCE = Path(__file__).stem
LOCAL_TIMESTAMP_CLOCK_ORIGINAL_DATETIME = datetime.datetime


class LedgerTimestampAwareClock(datetime.datetime):
    current_instant = LOCAL_TIMESTAMP_CLOCK_INSTANTS[0]

    @classmethod
    def now(cls, tz=None):
        assert tz is datetime.timezone.utc, "ledger clock must start with an aware UTC instant"
        return cls.current_instant.astimezone(tz)


def test_ledger_local_timestamp_fresh_aware_clock():
    previous_timezone = os.environ.get("TZ")
    try:
        with tempfile.TemporaryDirectory() as timestamp_directory:
            timestamp_ledger = ledger.SpendLedger(Path(timestamp_directory) / LOCAL_TIMESTAMP_CLOCK_DATABASE)
            try:
                for timezone_name in LOCAL_TIMESTAMP_CLOCK_ZONES:
                    os.environ["TZ"] = timezone_name
                    time.tzset()
                    for instant in LOCAL_TIMESTAMP_CLOCK_INSTANTS:
                        LedgerTimestampAwareClock.current_instant = instant
                        expected_local = instant.astimezone()
                        with patch.object(ledger.datetime, "datetime", LedgerTimestampAwareClock):
                            event_id = timestamp_ledger.record_event({
                                "source": LOCAL_TIMESTAMP_CLOCK_SOURCE,
                                "dedup_key": f"{timezone_name}:{instant.isoformat()}",
                                "batch_usd": LOCAL_TIMESTAMP_CLOCK_AMOUNT,
                            })
                        stored = timestamp_ledger.get(event_id)
                        assert stored["local_datetime"] == expected_local.isoformat(timespec="seconds")
                        assert stored["tz"] == (getattr(expected_local.tzinfo, "key", None)
                                                or expected_local.tzname() or "")
                        assert stored["recorded_at"] == instant.isoformat(timespec="seconds")
                        assert LOCAL_TIMESTAMP_CLOCK_ORIGINAL_DATETIME.fromisoformat(
                            stored["local_datetime"]).astimezone(datetime.timezone.utc) == instant
            finally:
                if timestamp_ledger._conn is not None:
                    timestamp_ledger._conn.close()
    finally:
        if previous_timezone is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = previous_timezone
        time.tzset()


if __name__ == "__main__":
    test_ledger_local_timestamp_fresh_aware_clock()
