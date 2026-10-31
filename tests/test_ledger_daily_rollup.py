"""Offline exact-money, grain, mutation and snapshot guards for daily memoization."""
import contextlib
import datetime
import os
import pathlib
import sqlite3
import sys
import tempfile
import traceback
import time
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import Mock, patch

from _pytest_fixture_bridge import align_test_startup_home

align_test_startup_home(prefix=__name__)

import spendguard
from spendguard import budget, config, ledger

DAILY_GUARD_ROWS = 20000
DAILY_GUARD_CLOSED_DAYS = 4
DAILY_GUARD_RATIO_DENOMINATOR = 100
DAILY_GUARD_SOURCE = "daily-rollup-regression"
DAILY_GUARD_RACE_SOURCE = "daily-rollup-concurrent-regression"
DAILY_GUARD_DATABASE = "daily-rollup.sqlite"
DAILY_GUARD_ROLLUP_TABLE = "spend_daily_rollup"
DAILY_GUARD_FINGERPRINT_TABLE = "rollup_day_fingerprint"
DAILY_GUARD_AMOUNTS = ("0.0000000006", "0.1234567894", "1.0000000005")
DAILY_GUARD_MUTATION_AMOUNT = "3.1415926535"
DAILY_GUARD_FOREIGN_CURRENCY = "EUR"
DAILY_GUARD_CANONICAL_CURRENCY = "USD"
DAILY_GUARD_META_PERIOD = 2
DAILY_GUARD_RECONCILED_PERIOD = 7
DAILY_GUARD_VOID_PERIOD = 11
DAILY_GUARD_RECONSTRUCTED_PERIOD = 13
DAILY_GUARD_FOREIGN_PERIOD = 17
DAILY_GUARD_MIRROR_PERIOD = 19
DAILY_GUARD_SCHEMA_REOPENS = 2
DAILY_GUARD_ARITHMETIC_MUTATION_NANOS = 1
DAILY_GUARD_BOUNDARY_SHIFTS = (1, -1, 0)
DAILY_GUARD_DIRTY_TABLE = "rollup_dirty_day"
DAILY_GUARD_HOT_READ_BOUND_SECONDS = 0.5
DAILY_GUARD_BUSY_MESSAGES = ("connection busy", "expiry deferred")
DAILY_GUARD_REPORTING_ZONES = ("UTC", "Pacific/Honolulu")


@contextlib.contextmanager
def daily_guard_fixture(row_count=DAILY_GUARD_ROWS):
    """A realistic low-flag, high-conversation-cardinality ledger, never live data."""
    with tempfile.TemporaryDirectory(dir=config.HOME) as directory:
        path = pathlib.Path(directory) / DAILY_GUARD_DATABASE
        writer = ledger.SpendLedger(path, owned_connection=config.open_ledger_connection(path))
        today = datetime.datetime.now(datetime.timezone.utc).date()
        days = tuple((today - datetime.timedelta(days=offset)).isoformat()
                     for offset in range(DAILY_GUARD_CLOSED_DAYS + 1))
        seed = writer.record_event({"source": DAILY_GUARD_SOURCE, "day": days[-1],
                                    "realtime_usd": DAILY_GUARD_AMOUNTS[1]})
        original = dict(writer._conn.execute("SELECT * FROM spend_events WHERE id=?", (seed,)).fetchone())
        columns = tuple(original)
        insert = ("INSERT INTO spend_events (" + ",".join(columns) + ") VALUES ("
                  + ",".join("?" for _ in columns) + ")")
        writer._conn.execute("DELETE FROM spend_events")
        nano_column = ledger.SPEND_EVENT_NANO_COLUMNS["realtime_usd"]
        external_nano = ledger.SPEND_EVENT_NANO_COLUMNS["external_usd"]

        def daily_guard_rows():
            for index in range(row_count):
                row = dict(original)
                row.update(id=str(index), day=days[index % len(days)],
                           conv_id=str(index), chain=str(index),
                           realtime_usd=DAILY_GUARD_AMOUNTS[index % len(DAILY_GUARD_AMOUNTS)],
                           external_usd=DAILY_GUARD_AMOUNTS[index % len(DAILY_GUARD_AMOUNTS)],
                           is_meta=index % DAILY_GUARD_META_PERIOD,
                           reconciled=int(index % DAILY_GUARD_RECONCILED_PERIOD == 0),
                           status="void" if index % DAILY_GUARD_VOID_PERIOD == 0 else "posted",
                           cost_basis="reconstructed" if index % DAILY_GUARD_RECONSTRUCTED_PERIOD == 0 else "metered",
                           currency=(DAILY_GUARD_FOREIGN_CURRENCY if index % DAILY_GUARD_FOREIGN_PERIOD == 0
                                     else DAILY_GUARD_CANONICAL_CURRENCY))
                # Exercise the exact heap's SQL ROUND fallback, not Python rounding.
                row[nano_column] = (index if index % DAILY_GUARD_MIRROR_PERIOD == 0 else None)
                row[external_nano] = None
                yield tuple(row[column] for column in columns)

        writer._conn.executemany(insert, daily_guard_rows())
        writer._conn.commit()
        with patch.object(config, "db_path", return_value=path), \
                patch.object(budget._LEDGER_TL, "led", writer, create=True):
            try:
                yield writer, days
            finally:
                observer = getattr(budget._LEDGER_TL, "projection_observer", None)
                if observer is not None:
                    observer[1].__exit__(None, None, None)
                    del budget._LEDGER_TL.projection_observer
                writer._conn.close()


def daily_guard_expected(writer, since=None, until=None):
    options = {"since": since, "until": until}
    return (
        float(writer._cat_dec(ledger.LLM_USD_COLS, writer._COUNTABLE, **options)),
        round(float(writer._cat_dec(("external_usd",),
              writer._LIVE_FILTER + " AND " + writer._NOT_UNPRICED,
              where={"is_meta": 0}, **options)), 6),
        float(writer._cat_dec(ledger.LLM_USD_COLS,
              "COALESCE(is_meta,0)=1 AND " + writer._NOT_VOID, **options)),
    )


def daily_guard_roots(since):
    return (budget.spent_since(since), budget.external_spent_since(since), budget.meta_spent_since(since))


def daily_guard_assert_root_reconciliation(writer, since):
    assert daily_guard_roots(since) == daily_guard_expected(writer, since)


def daily_guard_tables(writer):
    return {row[0] for row in writer._conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def daily_guard_verified_cache(writer, since):
    """Prove the exact result is served by the aggregate implementation itself."""
    from spendguard.ledger_daily_rollup import read_daily_rollup_category
    budget.spent_since(since)
    budget.spent_since(since)
    with budget.budget_projection_reader() as snapshot:
        result = read_daily_rollup_category(snapshot, ledger.LLM_USD_COLS,
                                           writer._COUNTABLE, since=since)
    assert result is not None, "warm supported window still takes the heap path"
    assert float(result) == daily_guard_expected(writer, since)[0]


def check_daily_rollup_reconciles_to_heap_every_root():
    with daily_guard_fixture() as (writer, days):
        deltas = []
        for day in (*days, None):
            expected = daily_guard_expected(writer, day)
            for actual, oracle in zip(daily_guard_roots(day), expected):
                deltas.append(abs(Decimal(str(actual)) - Decimal(str(oracle))))
        assert budget.spent_all_time() == daily_guard_expected(writer)[0]
        assert max(deltas) == 0, deltas
        daily_guard_verified_cache(writer, days[-1])
        # Scoped high-cardinality readers remain exact heap fallbacks.
        for conversation in (str(1), str(DAILY_GUARD_ROWS - 1)):
            assert budget.spent_since(days[-1], conv=conversation) == float(writer._cat_dec(
                ledger.LLM_USD_COLS, writer._COUNTABLE, since=days[-1], where={"conv_id": conversation}))
        from spendguard import ledger_daily_rollup
        mutated = format(Decimal(writer._cat_dec(ledger.LLM_USD_COLS, writer._COUNTABLE))
                         + Decimal(DAILY_GUARD_ARITHMETIC_MUTATION_NANOS) / ledger.NANO_USD_PER_USD, "f")
        original_reader = ledger_daily_rollup.read_daily_rollup_category

        def daily_guard_mutate_snapshot_arithmetic(candidate, *args, **kwargs):
            if (getattr(candidate, "_budget_projection_snapshot", False)
                    and args[0] == ledger.LLM_USD_COLS and args[1] == writer._COUNTABLE):
                return mutated
            return original_reader(candidate, *args, **kwargs)

        with patch.object(ledger_daily_rollup, "read_daily_rollup_category",
                          side_effect=daily_guard_mutate_snapshot_arithmetic):
            try:
                daily_guard_assert_root_reconciliation(writer, None)
            except AssertionError:
                print("  arithmetic mutation detected: one nano-USD divergence")
            else:
                raise AssertionError("reconciliation guard missed a one-nano arithmetic mutation")
        print(f"  maximum reconciliation delta={max(deltas)}; fixture rows={DAILY_GUARD_ROWS}")


def check_daily_rollup_window_boundaries_exact():
    with daily_guard_fixture() as (writer, days):
        for day in days:
            assert daily_guard_roots(day) == daily_guard_expected(writer, day)
            assert float(writer.spent_dec(since=day, until=day)) == daily_guard_expected(writer, day, day)[0]
        for operation in (budget.spent_since, budget.external_spent_since, budget.meta_spent_since):
            try:
                operation(days[-1] + "T12:00:00+00:00")
            except ledger.LedgerDateBoundaryError:
                pass
            else:
                raise AssertionError("timestamp boundary accepted")
        with patch.dict(os.environ, {"SPENDGUARD_REPORTING_TZ": "Pacific/Honolulu"}):
            assert ledger._reporting_tz() == os.environ["SPENDGUARD_REPORTING_TZ"]
        daily_guard_verified_cache(writer, days[-1])
        from spendguard import ledger_daily_rollup
        original_open_day = datetime.date.fromisoformat(days[0])
        unchanged_heap = tuple(tuple(row) for row in writer._conn.execute("SELECT * FROM spend_events ORDER BY id"))
        # Rollover changes the closed-day set even when no spend writer commits.
        # Moving backward also exercises a reporting-zone date change.
        for shift in DAILY_GUARD_BOUNDARY_SHIFTS:
            shifted_open_day = (original_open_day + datetime.timedelta(days=shift)).isoformat()
            with patch.object(ledger_daily_rollup, "reporting_rollup_open_day", return_value=shifted_open_day):
                for reporting_zone in DAILY_GUARD_REPORTING_ZONES:
                    with patch.dict(os.environ, {"SPENDGUARD_REPORTING_TZ": reporting_zone}):
                        daily_guard_assert_root_reconciliation(writer, None)
                        daily_guard_verified_cache(writer, days[-1])
                        daily_guard_assert_root_reconciliation(writer, None)
        assert tuple(tuple(row) for row in writer._conn.execute("SELECT * FROM spend_events ORDER BY id")) == unchanged_heap
        print("  no-write midnight rollover and reporting-zone changes remain exact")


def check_daily_rollup_backdated_insert_invalidates_day():
    with daily_guard_fixture() as (writer, days):
        daily_guard_verified_cache(writer, days[-1])
        before = budget.spent_all_time()
        identifier = writer.record_event({"source": DAILY_GUARD_SOURCE, "day": days[-1],
                                          "realtime_usd": DAILY_GUARD_MUTATION_AMOUNT})
        assert budget.spent_all_time() != before
        assert daily_guard_roots(days[-1]) == daily_guard_expected(writer, days[-1])
        daily_guard_verified_cache(writer, days[-1])
        nano = ledger.SPEND_EVENT_NANO_COLUMNS["realtime_usd"]
        writer._conn.execute(f"UPDATE spend_events SET {nano}={nano}+1 WHERE id=?", (identifier,))
        writer._conn.commit()
        assert daily_guard_roots(days[-1]) == daily_guard_expected(writer, days[-1])
        writer._conn.execute("DELETE FROM spend_events WHERE id=?", (identifier,))
        writer._conn.commit()
        assert daily_guard_roots(days[-1]) == daily_guard_expected(writer, days[-1])



def check_daily_rollup_today_write_does_not_revalidate_closed_days():
    from spendguard import ledger_daily_rollup
    with daily_guard_fixture() as (writer, days):
        daily_guard_verified_cache(writer, days[-1])
        # The pre-dirty-marker implementation re-fingerprints all closed days after
        # a TODAY write. Keep its fingerprint spy as a compatibility probe so
        # the fail-before result demonstrates the actual invalidation defect.
        recompute_name = ("recompute_rollup_day" if hasattr(ledger_daily_rollup, "recompute_rollup_day")
                          else "spend_daily_rollup_digest")
        with patch.object(ledger_daily_rollup, recompute_name,
                          wraps=getattr(ledger_daily_rollup, recompute_name)) as recompute, \
                patch.object(ledger_daily_rollup, "rollup_fingerprint_audit", create=True) as audit, \
                patch("logging.Logger.info") as info_log, patch("logging.Logger.warning") as warning_log:
            writer.record_event({"source": DAILY_GUARD_SOURCE, "day": days[0],
                                 "realtime_usd": DAILY_GUARD_MUTATION_AMOUNT})
            started = time.monotonic()
            actual = budget.spent_all_time()
            elapsed = time.monotonic() - started
            assert recompute.call_count == 0, recompute.call_count
            assert audit.call_count == 0, audit.call_args_list
            assert elapsed < DAILY_GUARD_HOT_READ_BOUND_SECONDS, elapsed
            logged = " ".join(str(call).lower() for call in (*info_log.call_args_list,
                                                            *warning_log.call_args_list))
            assert not any(message in logged for message in DAILY_GUARD_BUSY_MESSAGES), logged
        assert actual == daily_guard_expected(writer)[0]
        assert not list(writer._conn.execute(f"SELECT day FROM {DAILY_GUARD_DIRTY_TABLE}"))
        print(f"  post-TODAY-write read={elapsed * 1000:.3f} ms; closed recomputations=0")


def check_daily_rollup_backdated_write_dirties_only_that_day():
    from spendguard import ledger_daily_rollup
    with daily_guard_fixture() as (writer, days):
        daily_guard_verified_cache(writer, days[-1])
        writer.record_event({"source": DAILY_GUARD_SOURCE, "day": days[-1],
                             "realtime_usd": DAILY_GUARD_MUTATION_AMOUNT})
        assert [row[0] for row in writer._conn.execute(
            f"SELECT day FROM {DAILY_GUARD_DIRTY_TABLE}")] == [days[-1]], list(writer._conn.execute(f"SELECT * FROM {DAILY_GUARD_DIRTY_TABLE}"))
        original_reader = ledger_daily_rollup.read_daily_rollup_category
        accelerated_results = []

        def daily_guard_require_dirty_aggregate(candidate, *args, **kwargs):
            result = original_reader(candidate, *args, **kwargs)
            accelerated_results.append(result)
            assert result is not None, "dirty-day read fell through to a full category heap scan"
            return result

        with patch.object(ledger_daily_rollup, "recompute_rollup_day",
                          wraps=ledger_daily_rollup.recompute_rollup_day) as recompute, \
                patch.object(ledger_daily_rollup, "read_daily_rollup_category",
                             side_effect=daily_guard_require_dirty_aggregate):
            actual = budget.spent_all_time()
        assert accelerated_results, "dirty-day aggregate was not dispatched"
        assert [call.args[1] for call in recompute.call_args_list] == [days[-1]]
        assert actual == daily_guard_expected(writer)[0]
        assert not list(writer._conn.execute(f"SELECT day FROM {DAILY_GUARD_DIRTY_TABLE}"))


def check_daily_rollup_concurrent_backdated_write_not_lost():
    from spendguard import ledger_daily_rollup
    with daily_guard_fixture() as (writer, days):
        daily_guard_verified_cache(writer, days[-1])
        writer.record_event({"source": DAILY_GUARD_SOURCE, "day": days[-1],
                             "realtime_usd": DAILY_GUARD_MUTATION_AMOUNT})
        before_race = daily_guard_expected(writer)[0]
        original_recompute = ledger_daily_rollup.recompute_rollup_day
        raced = []

        def daily_guard_race_dirty_recomputation(candidate, day):
            rows = original_recompute(candidate, day)
            if not raced:
                before_count = writer._conn.execute("SELECT COUNT(*) FROM spend_events").fetchone()[0]
                raced.append(writer.record_event({"source": DAILY_GUARD_RACE_SOURCE, "day": day,
                                                  "realtime_usd": DAILY_GUARD_MUTATION_AMOUNT}))
                assert writer._conn.execute("SELECT COUNT(*) FROM spend_events").fetchone()[0] > before_count
            return rows

        with patch.object(ledger_daily_rollup, "recompute_rollup_day",
                          side_effect=daily_guard_race_dirty_recomputation):
            assert budget.spent_all_time() == before_race
        assert raced, "concurrent writer was not interleaved"
        assert [row[0] for row in writer._conn.execute(
            f"SELECT day FROM {DAILY_GUARD_DIRTY_TABLE}")] == [days[-1]], list(writer._conn.execute(f"SELECT * FROM {DAILY_GUARD_DIRTY_TABLE}"))
        assert budget.spent_all_time() == daily_guard_expected(writer)[0]
        assert not list(writer._conn.execute(f"SELECT day FROM {DAILY_GUARD_DIRTY_TABLE}"))


def check_daily_rollup_audit_runs_off_hot_path():
    from spendguard import ledger_daily_rollup
    with daily_guard_fixture() as (writer, days):
        daily_guard_verified_cache(writer, days[-1])
        original_recompute = ledger_daily_rollup.recompute_rollup_day
        observed_snapshots = []

        def daily_guard_observe_audit_snapshot(candidate, day):
            observed_snapshots.append(getattr(candidate, "_budget_projection_snapshot", False))
            return original_recompute(candidate, day)

        # Corrupt both stored measure and its checksum: a memo-integrity check
        # alone cannot detect this drift away from the authoritative heap.
        nano = ledger.SPEND_EVENT_NANO_COLUMNS["realtime_usd"]
        writer._conn.execute(f"UPDATE {DAILY_GUARD_ROLLUP_TABLE} SET {nano}='0' WHERE day=?", (days[-1],))
        dimensions = ",".join(ledger_daily_rollup.DAILY_ROLLUP_DIMENSIONS)
        money_columns = ",".join(ledger.SPEND_EVENT_NANO_COLUMNS.values())
        corrupted_rows = [tuple(row) for row in writer._conn.execute(
            f"SELECT day,{dimensions},{money_columns},event_count,max_event_rowid "
            f"FROM {DAILY_GUARD_ROLLUP_TABLE} WHERE day=?", (days[-1],))]
        writer._conn.execute(f"UPDATE {DAILY_GUARD_FINGERPRINT_TABLE} SET fingerprint=? WHERE day=?",
                             (ledger_daily_rollup.spend_daily_rollup_digest(corrupted_rows), days[-1]))
        writer._conn.commit()
        with patch.object(ledger_daily_rollup, "recompute_rollup_day",
                          side_effect=daily_guard_observe_audit_snapshot) as recompute, \
                patch.object(ledger_daily_rollup._DAILY_ROLLUP_LOG, "warning") as warning_log, \
                patch.object(ledger_daily_rollup._DAILY_ROLLUP_LOG, "info") as info_log:
            ledger_daily_rollup.rollup_fingerprint_audit(writer, force=True)
            assert observed_snapshots and all(observed_snapshots), observed_snapshots
            assert days[-1] in [call.args[1] for call in recompute.call_args_list]
            assert warning_log.called or info_log.called, "audit mismatch was not logged"
            assert budget.spent_all_time() == daily_guard_expected(writer)[0]
            recompute.reset_mock()
            with patch.object(config, "spend_daily_rollup_audit_interval_seconds",
                              return_value=DAILY_GUARD_HOT_READ_BOUND_SECONDS):
                ledger_daily_rollup.rollup_fingerprint_audit(writer)
                assert recompute.call_count == 0, "maintenance ignored its cadence"
        with patch.object(ledger_daily_rollup, "rollup_fingerprint_audit") as audit:
            daily_guard_roots(days[-1])
            assert audit.call_count == 0, "foreground read invoked full audit"


def check_daily_rollup_audit_refusal_keeps_discovered_drift_dirty():
    from spendguard import ledger_daily_rollup
    with daily_guard_fixture() as (writer, days):
        daily_guard_verified_cache(writer, days[-1])
        nano = ledger.SPEND_EVENT_NANO_COLUMNS["realtime_usd"]
        # Simulate corruption outside trigger coverage, then refuse repair.
        writer._conn.execute(f"UPDATE {ledger_daily_rollup.ROLLUP_STATE_TABLE} SET publishing=1")
        writer._conn.execute(f"UPDATE {DAILY_GUARD_ROLLUP_TABLE} SET {nano}='0' WHERE day=?", (days[-1],))
        writer._conn.execute(f"UPDATE {ledger_daily_rollup.ROLLUP_STATE_TABLE} SET publishing=0")
        writer._conn.commit()
        assert not writer._conn.execute(f"SELECT day FROM {DAILY_GUARD_DIRTY_TABLE}").fetchall()
        with patch.object(ledger_daily_rollup, "_publish_spend_daily_memo"):
            ledger_daily_rollup.rollup_fingerprint_audit(writer, force=True)
        marked = {row[0] for row in writer._conn.execute(f"SELECT day FROM {DAILY_GUARD_DIRTY_TABLE}")}
        assert days[-1] in marked, marked
        assert budget.spent_all_time() == daily_guard_expected(writer)[0]


def check_daily_rollup_fallback_on_missing_or_corrupt_rollup():
    with daily_guard_fixture() as (writer, days):
        from spendguard import ledger_daily_rollup
        daily_guard_verified_cache(writer, days[-1])
        nano = ledger.SPEND_EVENT_NANO_COLUMNS["realtime_usd"]
        for mutation in (f"UPDATE {DAILY_GUARD_ROLLUP_TABLE} SET {nano}='NaN'",
                         f"DELETE FROM {DAILY_GUARD_ROLLUP_TABLE} WHERE rowid=(SELECT MIN(rowid) FROM {DAILY_GUARD_ROLLUP_TABLE})"):
            writer._conn.execute(mutation)
            writer._conn.commit()
            with patch.object(ledger_daily_rollup._DAILY_ROLLUP_LOG, "info") as refresh_log:
                assert daily_guard_roots(days[-1]) == daily_guard_expected(writer, days[-1])
                assert refresh_log.called, "corrupt or missing cache must log degradation"
            daily_guard_verified_cache(writer, days[-1])
        for table in (DAILY_GUARD_ROLLUP_TABLE, DAILY_GUARD_FINGERPRINT_TABLE):
            writer._conn.execute(f"DROP TABLE IF EXISTS {table}")
            writer._conn.commit()
            assert daily_guard_roots(days[-1]) == daily_guard_expected(writer, days[-1])
        with patch.object(ledger.SpendLedger, "_cat_dec", side_effect=sqlite3.OperationalError("injected heap failure")):
            try:
                budget.spent_since(days[-1])
            except Exception:
                pass
            else:
                raise AssertionError("underlying heap failure returned money")


def check_daily_rollup_cache_day_move_invalidates_both_days():
    with daily_guard_fixture() as (writer, days):
        daily_guard_verified_cache(writer, days[-1])
        displaced_day = (datetime.date.fromisoformat(days[-1])
                         - datetime.timedelta(days=DAILY_GUARD_CLOSED_DAYS)).isoformat()
        writer._conn.execute(
            f"UPDATE {DAILY_GUARD_ROLLUP_TABLE} SET day=? WHERE day=?",
            (displaced_day, days[-1]))
        writer._conn.commit()
        marked = {row[0] for row in writer._conn.execute(f"SELECT day FROM {DAILY_GUARD_DIRTY_TABLE}")}
        assert {displaced_day, days[-1]} <= marked, marked
        assert daily_guard_roots(None) == daily_guard_expected(writer)


def check_daily_rollup_reopen_missing_memo_rebuilds_boundary():
    for table in (DAILY_GUARD_ROLLUP_TABLE, DAILY_GUARD_FINGERPRINT_TABLE, DAILY_GUARD_DIRTY_TABLE):
        with daily_guard_fixture() as (writer, days):
            daily_guard_verified_cache(writer, days[-1])
            writer._conn.execute(f"DROP TABLE {table}")
            writer._conn.commit()
            reopened = ledger.SpendLedger(writer.db_path,
                                          owned_connection=config.open_ledger_connection(writer.db_path))
            try:
                assert daily_guard_roots(None) == daily_guard_expected(writer)
            finally:
                reopened._conn.close()


def check_daily_rollup_cold_rebuild_removes_aggregate_only_orphans():
    with daily_guard_fixture() as (writer, days):
        daily_guard_verified_cache(writer, days[-1])
        writer._conn.execute(f"DROP TABLE {DAILY_GUARD_FINGERPRINT_TABLE}")
        writer._conn.commit()
        reopened = ledger.SpendLedger(writer.db_path,
                                      owned_connection=config.open_ledger_connection(writer.db_path))
        try:
            # The partial loss resets the trusted frontier. Raw deletes during
            # that cold interval need no marker, but retained memos must vanish.
            reopened._conn.execute("DELETE FROM spend_events WHERE day=?", (days[-1],))
            reopened._conn.commit()
            assert reopened._conn.execute(
                f"SELECT COUNT(*) FROM {DAILY_GUARD_ROLLUP_TABLE} WHERE day=?",
                (days[-1],)).fetchone()[0] > 0
            assert daily_guard_roots(None) == daily_guard_expected(writer)
            assert reopened._conn.execute(
                f"SELECT COUNT(*) FROM {DAILY_GUARD_ROLLUP_TABLE} WHERE day=?",
                (days[-1],)).fetchone()[0] == 0
        finally:
            reopened._conn.close()


def check_daily_rollup_snapshot_semantics_preserved():
    for pin_before_first_rollup in (False, True):
        with daily_guard_fixture() as (writer, days):
            daily_guard_verified_cache(writer, days[-1])
            baseline = daily_guard_expected(writer)[0]
            with budget.budget_projection_reader() as snapshot:
                if pin_before_first_rollup:
                    # Any earlier consumer can pin SQLite's transaction before
                    # this snapshot invokes its first money/rollup operation.
                    snapshot._conn.execute("SELECT COUNT(*) FROM spend_events").fetchone()
                    first = baseline
                else:
                    first = snapshot.spent_dec(deadline_s=None)
                writer.record_event({"source": DAILY_GUARD_SOURCE, "day": days[-1],
                                     "realtime_usd": DAILY_GUARD_MUTATION_AMOUNT})
                assert float(snapshot.spent_dec(deadline_s=None)) == float(first) == baseline
            after_snapshot = budget.spent_since(None)
            expected_after_snapshot = daily_guard_expected(writer)[0]
            assert after_snapshot == expected_after_snapshot, (after_snapshot, expected_after_snapshot, baseline)
            print(f"  snapshot exact; arbitrary SQL pinned before first rollup={pin_before_first_rollup}")
    with daily_guard_fixture() as (writer, days):
        from spendguard import ledger_daily_rollup
        baseline = daily_guard_expected(writer)[0]
        interleaved_commits = []

        def daily_guard_open_publish_race(*args, **kwargs):
            real_connection = sqlite3.connect(*args, **kwargs)
            proxy = Mock(wraps=real_connection)

            def daily_guard_commit_then_backdated_writer():
                real_connection.commit()
                if not interleaved_commits:
                    interleaved_commits.append(writer.record_event({
                        "source": DAILY_GUARD_SOURCE, "day": days[-1],
                        "realtime_usd": DAILY_GUARD_MUTATION_AMOUNT}))

            proxy.commit.side_effect = daily_guard_commit_then_backdated_writer
            return proxy

        publish_driver = SimpleNamespace(connect=daily_guard_open_publish_race, Error=sqlite3.Error)
        with patch.object(ledger_daily_rollup, "sqlite3", publish_driver):
            # Force the first cold read to publish; interleave after its actual
            # COMMIT but before the publication helper can stamp verification.
            assert budget.spent_since(None) == baseline
        assert interleaved_commits, "publication COMMIT interleave did not execute"
        after_publish = daily_guard_expected(writer)[0]
        assert after_publish != baseline
        assert budget.spent_since(None) == after_publish
        daily_guard_verified_cache(writer, days[-1])
        print("  committed backdated write between cache COMMIT and verification remains exact")


def check_daily_rollup_migration_additive_idempotent():
    with daily_guard_fixture() as (writer, days):
        baseline = tuple(tuple(row) for row in writer._conn.execute("SELECT * FROM spend_events ORDER BY id"))
        daily_guard_roots(days[-1])
        assert DAILY_GUARD_ROLLUP_TABLE in daily_guard_tables(writer)
        assert DAILY_GUARD_FINGERPRINT_TABLE in daily_guard_tables(writer)
        assert DAILY_GUARD_DIRTY_TABLE in daily_guard_tables(writer)
        for _ in range(DAILY_GUARD_SCHEMA_REOPENS):
            reopened = ledger.SpendLedger(writer.db_path, owned_connection=config.open_ledger_connection(writer.db_path))
            reopened._conn.close()
        assert tuple(tuple(row) for row in writer._conn.execute("SELECT * FROM spend_events ORDER BY id")) == baseline


def check_daily_rollup_config_and_naming():
    with daily_guard_fixture() as (writer, days):
        daily_guard_roots(days[-1])
        columns = {row[1] for row in writer._conn.execute(f"PRAGMA table_info({DAILY_GUARD_ROLLUP_TABLE})")}
        assert columns, "rollup schema missing"
        assert not {"conv_id", "chain", "project_primary"}.intersection(columns), columns
        with patch.dict(os.environ, {"SPENDGUARD_REPORTING_TZ": "Pacific/Honolulu"}):
            assert config.spend_daily_rollup_reporting_timezone() == os.environ["SPENDGUARD_REPORTING_TZ"]


def check_daily_rollup_rollup_is_orders_of_magnitude_smaller_than_heap():
    with daily_guard_fixture() as (writer, days):
        daily_guard_roots(days[-1])
        rolled = writer._conn.execute(f"SELECT COUNT(*) FROM {DAILY_GUARD_ROLLUP_TABLE}").fetchone()[0]
        heap = writer._conn.execute("SELECT COUNT(*) FROM spend_events").fetchone()[0]
        assert 0 < rolled < heap / DAILY_GUARD_RATIO_DENOMINATOR, (rolled, heap)
        print(f"  rollup_rows={rolled}; heap_rows={heap}; ratio={rolled / heap}")


def run_daily_rollup_guards():
    spendguard.require()
    failures = []
    for name, operation in tuple(globals().items()):
        if name.startswith("check_daily_rollup_"):
            try:
                operation()
                print(f"[OK] {name}")
            except Exception:
                failures.append(name)
                print(f"[FAIL] {name}")
                traceback.print_exc()
    return bool(failures)


if __name__ == "__main__":
    sys.exit(run_daily_rollup_guards())
