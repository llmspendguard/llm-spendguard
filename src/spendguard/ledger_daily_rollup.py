"""Snapshot-owned, disposable closed-day aggregate memoization.

The heap remains authoritative. Transactional day markers invalidate closed-day
memos precisely; periodic snapshot audits retain defense against silent drift.
"""
import datetime
import hashlib
import json
import logging
import sqlite3
import threading
import time
from contextlib import closing
from types import SimpleNamespace
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo


SPEND_DAILY_ROLLUP_TABLE = "spend_daily_rollup"
ROLLUP_DAY_FINGERPRINT_TABLE = "rollup_day_fingerprint"
ROLLUP_DIRTY_DAY_TABLE = "rollup_dirty_day"
ROLLUP_STATE_TABLE = "rollup_cache_state"
ROLLUP_OPEN_DAY_SQL_FUNCTION = "spendguard_rollup_open_day"
_ROLLUP_AUDIT_ATTEMPTS = {}
_ROLLUP_AUDIT_THREADS = set()
_ROLLUP_AUDIT_CONTEXTS = {}
_ROLLUP_AUDIT_UNTRUSTED = set()
DAILY_ROLLUP_DIMENSIONS = ("is_meta", "reconciled", "status", "cost_basis", "currency")
_DAILY_ROLLUP_LOCK = threading.RLock()
_DAILY_ROLLUP_LOG = logging.getLogger(__name__)


def initialize_spend_daily_rollup(connection, nano_columns):
    """Add disposable aggregate tables without changing event schema or rows."""
    required_tables = (SPEND_DAILY_ROLLUP_TABLE, ROLLUP_DAY_FINGERPRINT_TABLE,
                       ROLLUP_DIRTY_DAY_TABLE, ROLLUP_STATE_TABLE)
    missing_memo = any(connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone() is None
        for table in required_tables)
    measures = ",".join(f"{column} TEXT" for column in nano_columns.values())
    dimensions = ",".join(f"{column}" for column in DAILY_ROLLUP_DIMENSIONS)
    connection.execute(f"CREATE TABLE IF NOT EXISTS {SPEND_DAILY_ROLLUP_TABLE} ("
                       f"day TEXT NOT NULL, grain_key TEXT NOT NULL, {dimensions}, {measures}, "
                       "event_count INTEGER NOT NULL, max_event_rowid INTEGER NOT NULL, "
                       "PRIMARY KEY(day,grain_key))")
    connection.execute(f"CREATE TABLE IF NOT EXISTS {ROLLUP_DAY_FINGERPRINT_TABLE} ("
                       "day TEXT PRIMARY KEY, fingerprint TEXT NOT NULL)")

    connection.execute(f"CREATE TABLE IF NOT EXISTS {ROLLUP_DIRTY_DAY_TABLE} ("
                       "day TEXT PRIMARY KEY, revision INTEGER NOT NULL)")
    connection.execute(f"CREATE TABLE IF NOT EXISTS {ROLLUP_STATE_TABLE} ("
                       "singleton INTEGER PRIMARY KEY, open_day TEXT, timezone TEXT, publishing INTEGER NOT NULL)")
    connection.execute(f"INSERT OR IGNORE INTO {ROLLUP_STATE_TABLE} VALUES (1,NULL,NULL,0)")
    if missing_memo:
        connection.execute(f"UPDATE {ROLLUP_STATE_TABLE} SET open_day=NULL,timezone=NULL WHERE singleton=1")


def initialize_rollup_dirty_triggers(connection):
    """Dirty only memoized closed days for every writer, including raw SQL.

    The persisted open-day frontier is sufficient even after midnight: days on
    or beyond it were never trusted and rollover recomputes them from the heap.
    """
    for action, references in (("INSERT", ("NEW",)), ("UPDATE", ("OLD", "NEW")), ("DELETE", ("OLD",))):
        body = " ".join(
            f"INSERT INTO {ROLLUP_DIRTY_DAY_TABLE}(day,revision) "
            f"SELECT {reference}.day,1 WHERE {reference}.day < "
            f"(SELECT open_day FROM {ROLLUP_STATE_TABLE} WHERE singleton=1) "
            "ON CONFLICT(day) DO UPDATE SET revision=revision+1;" for reference in references)
        connection.execute(f"DROP TRIGGER IF EXISTS rollup_dirty_event_{action.lower()}")
        connection.execute(f"CREATE TRIGGER rollup_dirty_event_{action.lower()} "
                           f"AFTER {action} ON spend_events BEGIN {body} END")
    # Cache tampering also fails closed without hashing every historical day.
    for table, label in ((SPEND_DAILY_ROLLUP_TABLE, "cache"), (ROLLUP_DAY_FINGERPRINT_TABLE, "fingerprint")):
        for action, references in (("UPDATE", ("OLD", "NEW")), ("DELETE", ("OLD",)), ("INSERT", ("NEW",))):
            trigger = f"rollup_dirty_{label}_{action.lower()}"
            connection.execute(f"DROP TRIGGER IF EXISTS {trigger}")
            body = " ".join(
                f"INSERT INTO {ROLLUP_DIRTY_DAY_TABLE}(day,revision) VALUES ({reference}.day,1) "
                "ON CONFLICT(day) DO UPDATE SET revision=revision+1;" for reference in references)
            connection.execute(f"CREATE TRIGGER {trigger} AFTER {action} ON {table} "
                               f"WHEN (SELECT publishing FROM {ROLLUP_STATE_TABLE} WHERE singleton=1)=0 "
                               f"BEGIN {body} END")


def mark_rollup_day_dirty(connection, day, open_day):
    """Bounded marker primitive for ingestion owners outside SQL triggers."""
    if day is not None and day < open_day:
        connection.execute(f"INSERT INTO {ROLLUP_DIRTY_DAY_TABLE}(day,revision) VALUES (?,1) "
                           "ON CONFLICT(day) DO UPDATE SET revision=revision+1", (day,))


def spend_daily_rollup_digest(rows):
    """Fingerprint complete grouped dimensions, count, max rowid and exact measures."""
    return hashlib.sha256(json.dumps(sorted(rows, key=repr), separators=(",", ":"),
                                    ensure_ascii=True).encode()).hexdigest()


def reporting_rollup_open_day(timezone_name):
    """Use the same reporting-zone setting as ledger accounting-day derivation."""
    return datetime.datetime.now(datetime.timezone.utc).astimezone(ZoneInfo(timezone_name)).date().isoformat()



def recompute_rollup_day(ledger, day):
    """Compute one complete exact closed-day fingerprint unit on its snapshot."""
    money = ledger._daily_rollup_money_columns
    projections = ",".join(f"dec_sum({ledger._daily_rollup_nano_expression(column)})" for column in money)
    dimensions = ",".join(DAILY_ROLLUP_DIMENSIONS)
    return [tuple(row) for row in ledger._conn.execute(
        f"SELECT day,{dimensions},{projections},COUNT(*),MAX(rowid) FROM spend_events "
        f"WHERE day=? GROUP BY day,{dimensions}", (day,))]


def _publish_spend_daily_memo(ledger, generation, grouped_rows, *, dirty_revisions=None,
                              completed_boundary=None):
    """Compare-and-clear revisions under a writer lock; never wait for writers.

    The snapshot's rows remain valid for that reader even if a concurrent commit
    makes publication ineligible. Each dirty revision is compared before any
    replacement, and again on clear, preventing a lost backdated mutation.
    """
    money = ledger._daily_rollup_money_columns
    settings = ledger._daily_rollup_settings
    observer = ledger._budget_projection_generation_observer
    revisions = dirty_revisions or {}
    if not _DAILY_ROLLUP_LOCK.acquire(blocking=False):
        return
    try:
        if observer() != generation:
            return
        connection = sqlite3.connect(ledger.db_path, timeout=settings.SPEND_DAILY_ROLLUP_WRITE_TIMEOUT_S)
        try:
            connection.execute("BEGIN IMMEDIATE")
            if observer() != generation:
                connection.rollback()
                return
            initialize_spend_daily_rollup(connection, money)
            connection.execute(f"UPDATE {ROLLUP_STATE_TABLE} SET publishing=1 WHERE singleton=1")
            columns = ("day", "grain_key", *DAILY_ROLLUP_DIMENSIONS,
                       *money.values(), "event_count", "max_event_rowid")
            days = {row[0] for row in grouped_rows} | set(revisions)
            for day in days:
                revision = revisions.get(day)
                if revision is not None:
                    current = connection.execute(f"SELECT revision FROM {ROLLUP_DIRTY_DAY_TABLE} WHERE day=?",
                                                 (day,)).fetchone()
                    if current is None or current[0] != revision:
                        continue
                day_rows = [row for row in grouped_rows if row[0] == day]
                connection.execute(f"DELETE FROM {SPEND_DAILY_ROLLUP_TABLE} WHERE day=?", (day,))
                for row in day_rows:
                    grain_key = json.dumps(row[1:1 + len(DAILY_ROLLUP_DIMENSIONS)], separators=(",", ":"))
                    connection.execute(f"INSERT INTO {SPEND_DAILY_ROLLUP_TABLE} ({','.join(columns)}) "
                                       f"VALUES ({','.join('?' for _ in columns)})", (day, grain_key, *row[1:]))
                connection.execute(f"INSERT OR REPLACE INTO {ROLLUP_DAY_FINGERPRINT_TABLE} VALUES (?,?)",
                                   (day, spend_daily_rollup_digest(day_rows)))
                if revision is not None:
                    connection.execute(f"DELETE FROM {ROLLUP_DIRTY_DAY_TABLE} WHERE day=? AND revision=?",
                                       (day, revision))
            if completed_boundary is not None:
                connection.execute(f"UPDATE {ROLLUP_STATE_TABLE} SET open_day=?,timezone=? WHERE singleton=1",
                                   completed_boundary)
            connection.execute(f"UPDATE {ROLLUP_STATE_TABLE} SET publishing=0 WHERE singleton=1")
            connection.commit()
        finally:
            connection.close()
    finally:
        _DAILY_ROLLUP_LOCK.release()


def read_daily_rollup_category(ledger, cols, filt, since=None, until=None, where=None):
    """Trust unchanged closed days; recompute only transactionally dirty days."""
    settings = ledger._daily_rollup_settings
    if str(Path(ledger.db_path).resolve()) in _ROLLUP_AUDIT_UNTRUSTED:
        return None
    if (not getattr(ledger, "_budget_projection_snapshot", False)
            or set(where or {}) - set(DAILY_ROLLUP_DIMENSIONS)):
        return None
    open_day = reporting_rollup_open_day(settings.spend_daily_rollup_reporting_timezone())
    boundary = (open_day, settings.spend_daily_rollup_reporting_timezone())
    generation = getattr(ledger, "_daily_rollup_snapshot_generation", None)
    observer = ledger._budget_projection_generation_observer
    if generation is None or observer() != generation:
        return None
    ledger._conn.execute("SELECT day FROM spend_events LIMIT 1").fetchone()
    if observer() != generation:
        return None
    try:
        state = ledger._conn.execute(
            f"SELECT open_day,timezone FROM {ROLLUP_STATE_TABLE} WHERE singleton=1").fetchone()
        revisions = dict(ledger._conn.execute(
            f"SELECT day,revision FROM {ROLLUP_DIRTY_DAY_TABLE} WHERE day < ?", (open_day,)))
        cold = state is None or tuple(state) != boundary
        if cold:
            # Bootstrap, a reporting-zone change, and rollover close new days.
            # No mutation generation can select this full population branch.
            if state is not None and state[0] is not None and state[1] == boundary[1]:
                new_days = [row[0] for row in ledger._conn.execute(
                    "SELECT DISTINCT day FROM spend_events WHERE day>=? AND day<?", (state[0], open_day))]
            else:
                # A partial memo loss can retain aggregate-only days whose
                # heap rows were deleted while no valid frontier existed.
                # Inventory every retained memo day so rebuilding also removes
                # orphans and restores empty-day fingerprints.
                new_days = [row[0] for row in ledger._conn.execute(
                    "SELECT day FROM spend_events WHERE day<? UNION "
                    f"SELECT day FROM {SPEND_DAILY_ROLLUP_TABLE} WHERE day<? UNION "
                    f"SELECT day FROM {ROLLUP_DAY_FINGERPRINT_TABLE} WHERE day<?",
                    (open_day, open_day, open_day))]
            dirty_days = sorted(set(new_days) | set(revisions))
        else:
            dirty_days = sorted(revisions)
        # Missing tables always fail closed; the fingerprint table also records
        # empty/deleted days, so its existence is checked even without dirtiness.
        ledger._conn.execute(f"SELECT day FROM {ROLLUP_DAY_FINGERPRINT_TABLE} LIMIT 1").fetchone()
        if dirty_days or cold:
            if dirty_days:
                _DAILY_ROLLUP_LOG.info("daily rollup dirty or missing; refreshing %s closed days", len(dirty_days))
            rows = [row for day in dirty_days for row in recompute_rollup_day(ledger, day)]
            try:
                # A today-only ledger needs no closed-day cache publication.
                # Avoid a cache-only commit invalidating the running-total memo.
                if dirty_days:
                    _publish_spend_daily_memo(ledger, generation, rows,
                                              dirty_revisions={day: revisions.get(day) for day in dirty_days},
                                              completed_boundary=boundary if cold else None)
            except (sqlite3.Error, OSError, ValueError) as failure:
                _DAILY_ROLLUP_LOG.warning("daily rollup refresh degraded: %s", failure)
            # Overlay complete recomputed rows on this pinned snapshot: cache
            # publication is invisible here and may have lost its CAS race.
            return _read_verified_daily_category(ledger, cols, filt, since, until, where,
                                                 dirty_days, open_day, overlay_rows=rows)
        return _read_verified_daily_category(ledger, cols, filt, since, until, where, None, open_day)
    except (sqlite3.Error, ValueError, TypeError, ArithmeticError) as failure:
        _DAILY_ROLLUP_LOG.info("daily rollup missing or stale; using authoritative heap: %s", failure)
        return None

def _read_verified_daily_category(ledger, cols, filt, since, until, where, days, open_day, overlay_rows=None):
    """Combine trusted closed rows, recomputed snapshot rows, and live today."""
    SPEND_EVENT_NANO_COLUMNS = ledger._daily_rollup_money_columns
    spend_event_nano_expression = ledger._daily_rollup_nano_expression
    spend_event_nano_sum_to_decimal = ledger._daily_rollup_nano_to_decimal
    try:
        predicates = ["day < ?"]
        parameters = [open_day]
        if since:
            predicates.append("day >= ?")
            parameters.append(since)
        if until:
            predicates.append("day <= ?")
            parameters.append(until)
        if filt:
            predicates.append(filt)
        for column, value in (where or {}).items():
            predicates.append(f"{column}=?")
            parameters.append(value)
        sums = ','.join(f"dec_sum({SPEND_EVENT_NANO_COLUMNS[column]})" for column in cols)
        rollup_source = SPEND_DAILY_ROLLUP_TABLE
        prefix_sql = ""
        overlay_parameters = []
        if overlay_rows is not None:
            overlay_columns = ("day", *DAILY_ROLLUP_DIMENSIONS, *SPEND_EVENT_NANO_COLUMNS.values(),
                               "event_count", "max_event_rowid")
            source_columns = ",".join(overlay_columns)
            cache_select = f"SELECT {source_columns} FROM {SPEND_DAILY_ROLLUP_TABLE}"
            if days:
                cache_select += f" WHERE day NOT IN ({','.join('?' for _ in days)})"
                overlay_parameters.extend(days)
            rollup_source = "effective_daily_rollup"
            if overlay_rows:
                row_values = ",".join("(" + ",".join("?" for _ in row) + ")" for row in overlay_rows)
                # VALUES has no compound-SELECT term limit. All recomputed rows
                # are retained; no sampling or evidence truncation is involved.
                prefix_sql = (f"WITH recomputed_daily_rollup({source_columns}) AS (VALUES {row_values}), "
                              f"{rollup_source}({source_columns}) AS (" + cache_select +
                              " UNION ALL SELECT * FROM recomputed_daily_rollup) ")
                overlay_parameters = [value for row in overlay_rows for value in row] + overlay_parameters
            else:
                prefix_sql = f"WITH {rollup_source}({source_columns}) AS (" + cache_select + ") "
        closed = ledger._conn.execute(prefix_sql + f"SELECT {sums} FROM {rollup_source} WHERE " +
                                      " AND ".join(predicates), [*overlay_parameters, *parameters]).fetchone()
        w, args = ledger._where(since, until, where)
        if filt:
            w += " AND " + filt
        live_sums = ','.join(f"dec_sum({spend_event_nano_expression(column)})" for column in cols)
        live = ledger._conn.execute(f"SELECT {live_sums} FROM spend_events WHERE "
                                    "(day >= ? OR day IS NULL)" + w, [open_day, *args]).fetchone()
        nano_totals = [int(a or 0) + int(b or 0) for a, b in zip(closed, live)]
        result = sum((spend_event_nano_sum_to_decimal(value) for value in nano_totals), Decimal(0))
        if not result.is_finite():
            raise ValueError("non-finite daily aggregate")
        return format(result, 'f')
    except (sqlite3.Error, ValueError, TypeError, ArithmeticError) as failure:
        _DAILY_ROLLUP_LOG.warning("daily rollup degraded; using authoritative heap: %s", failure)
        return None


def register_rollup_audit_context(ledger, snapshot_connection):
    """Inject money and snapshot contracts without retaining a mutable writer."""
    context = SimpleNamespace(
        db_path=str(Path(ledger.db_path).resolve()), _daily_rollup_settings=ledger._daily_rollup_settings,
        _daily_rollup_money_columns=ledger._daily_rollup_money_columns,
        _daily_rollup_nano_expression=ledger._daily_rollup_nano_expression,
        _daily_rollup_snapshot_connection=snapshot_connection)
    with _DAILY_ROLLUP_LOCK:
        _ROLLUP_AUDIT_CONTEXTS[context.db_path] = context


def rollup_fingerprint_audit(ledger, force=False):
    """Maintenance-only full fingerprint sweep using a ledger_read WAL snapshot."""
    settings = ledger._daily_rollup_settings
    key = str(Path(ledger.db_path).resolve())
    now = time.monotonic()
    with _DAILY_ROLLUP_LOCK:
        previous = _ROLLUP_AUDIT_ATTEMPTS.get(key)
        if not force and previous is not None and now - previous < settings.spend_daily_rollup_audit_interval_seconds():
            return False
        _ROLLUP_AUDIT_ATTEMPTS[key] = now
        context = _ROLLUP_AUDIT_CONTEXTS[key]
    with settings.managed_ledger_read_connection(ledger.db_path) as observer_connection:
        def observe_rollup_audit_generation():
            return observer_connection.execute("PRAGMA data_version").fetchone()[0]
        generation = observe_rollup_audit_generation()
        with context._daily_rollup_snapshot_connection(ledger.db_path) as connection:
            snapshot = SimpleNamespace(**vars(context))
            snapshot._conn = connection
            snapshot._budget_projection_snapshot = True
            snapshot._budget_projection_generation_observer = observe_rollup_audit_generation
            connection.execute("BEGIN")
            open_day = reporting_rollup_open_day(settings.spend_daily_rollup_reporting_timezone())
            days = {row[0] for row in connection.execute(
                "SELECT DISTINCT day FROM spend_events WHERE day<?", (open_day,))}
            days.update(row[0] for row in connection.execute(
                f"SELECT day FROM {ROLLUP_DAY_FINGERPRINT_TABLE} WHERE day<?", (open_day,)))
            days.update(row[0] for row in connection.execute(
                f"SELECT DISTINCT day FROM {SPEND_DAILY_ROLLUP_TABLE} WHERE day<?", (open_day,)))
            revisions = dict(connection.execute(f"SELECT day,revision FROM {ROLLUP_DIRTY_DAY_TABLE}"))
            fingerprints = dict(connection.execute(f"SELECT day,fingerprint FROM {ROLLUP_DAY_FINGERPRINT_TABLE}"))
            money = snapshot._daily_rollup_money_columns
            repaired = []
            repair_revisions = {}
            for day in sorted(days):
                rows = recompute_rollup_day(snapshot, day)
                cached = [tuple(row) for row in connection.execute(
                    f"SELECT day,{','.join(DAILY_ROLLUP_DIMENSIONS)},{','.join(money.values())},"
                    f"event_count,max_event_rowid FROM {SPEND_DAILY_ROLLUP_TABLE} WHERE day=?", (day,))]
                fingerprint = spend_daily_rollup_digest(rows)
                if fingerprints.get(day) != fingerprint or spend_daily_rollup_digest(cached) != fingerprint:
                    _DAILY_ROLLUP_LOG.warning("daily rollup fingerprint audit mismatch; recomputing day %s", day)
                    repaired.extend(rows)
                    # Include deleted days even when they have no aggregate rows.
                    repair_revisions[day] = revisions.get(day)
            if repair_revisions:
                _ROLLUP_AUDIT_UNTRUSTED.add(key)
                # Persist distrust before attempting an optimistic publication.
                # The marker transaction itself invalidates this snapshot's
                # generation; a refused publication therefore leaves the next
                # reader a durable instruction to recompute authoritative rows.
                with closing(sqlite3.connect(ledger.db_path,
                                            timeout=settings.SPEND_DAILY_ROLLUP_WRITE_TIMEOUT_S)) as marker:
                    marker.execute("BEGIN IMMEDIATE")
                    for day in repair_revisions:
                        marker.execute(f"INSERT INTO {ROLLUP_DIRTY_DAY_TABLE}(day,revision) VALUES (?,1) "
                                       "ON CONFLICT(day) DO UPDATE SET revision=revision+1", (day,))
                    marker.commit()
                _ROLLUP_AUDIT_UNTRUSTED.discard(key)
                _publish_spend_daily_memo(snapshot, generation, repaired, dirty_revisions=repair_revisions)
    return True


def schedule_rollup_fingerprint_audit(path=None):
    """Admit at most one maintenance worker per database, never sweep inline."""
    if path is None:
        return
    target = str(Path(path).resolve())
    if not _DAILY_ROLLUP_LOCK.acquire(blocking=False):
        return
    try:
        context = _ROLLUP_AUDIT_CONTEXTS.get(target)
        if context is None:
            return
        previous = _ROLLUP_AUDIT_ATTEMPTS.get(target)
        if previous is None:
            _ROLLUP_AUDIT_ATTEMPTS[target] = time.monotonic()
            return
        if target in _ROLLUP_AUDIT_THREADS or (previous is not None
                and time.monotonic() - previous < context._daily_rollup_settings.spend_daily_rollup_audit_interval_seconds()):
            return
        _ROLLUP_AUDIT_THREADS.add(target)
    finally:
        _DAILY_ROLLUP_LOCK.release()

    def run_rollup_fingerprint_maintenance():
        try:
            rollup_fingerprint_audit(context)
        except (sqlite3.Error, OSError, ValueError, TimeoutError) as failure:
            _DAILY_ROLLUP_LOG.warning("daily rollup fingerprint audit deferred: %s", failure)
        finally:
            with _DAILY_ROLLUP_LOCK:
                _ROLLUP_AUDIT_THREADS.discard(target)

    threading.Thread(target=run_rollup_fingerprint_maintenance, daemon=True,
                     name="spendguard-rollup-fingerprint-audit").start()
