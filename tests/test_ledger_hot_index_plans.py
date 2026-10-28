"""Hermetic plan and migration checks for hot ledger indexes."""
import os
import sqlite3
import sys
import tempfile


if not os.environ.get("SPENDGUARD_TEST_ISOLATED"):
    os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"
    os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="spendguard-hot-index-")
    os.execv(sys.executable, [sys.executable] + sys.argv)

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from _script_checks import record_script_check
from spendguard import calls, content_retention, guard, semcache
from spendguard.ledger import SpendLedger
from spendguard.schema_indexes import (
    _available_derived_index_name,
    ensure_ordered_index_coverage,
    table_index_coverages,
)


failures = []
_REPRESENTATIVE_ROWS = 30_000




def plan_details(connection, sql, parameters=()):
    return tuple(row[3] for row in connection.execute("EXPLAIN QUERY PLAN " + sql, parameters))


def plan_searches_with(plan, index_name):
    return any("SEARCH " in detail and index_name in detail for detail in plan)


calls_connection = sqlite3.connect(":memory:")
calls._ensure_call_observation_schema(calls_connection)
calls_connection.executemany(
    "INSERT INTO calls(id,ts,kind,provider,model,intent,cost,suspect) VALUES(?,?,?,?,?,?,?,?)",
    (
        (
            str(row_number),
            f"2026-10-{(row_number % 28) + 1:02d}T00:00:00+00:00",
            "batch" if row_number % 2 else "realtime",
            f"provider-{row_number % 5}",
            f"model-{row_number % 11}",
            f"intent-{row_number % 31}",
            1.0,
            None,
        )
        for row_number in range(_REPRESENTATIVE_ROWS)
    ),
)
calls_connection.execute("ANALYZE")
cost_index = next(
    name for columns, unique, name in table_index_coverages(calls_connection, "calls")
    if columns == ("kind", "ts") and not unique
)
cost_plan = plan_details(
    calls_connection,
    "SELECT model,date(ts),kind,COUNT(*) FROM calls WHERE ts>=? "
    "AND kind IN ('realtime','batch') AND cost IS NOT NULL AND suspect IS NULL "
    "GROUP BY model,date(ts),kind",
    ("2026-10-27T00:00:00+00:00",),
)
record_script_check(failures, "cost-integrity range is an index SEARCH", plan_searches_with(cost_plan, cost_index))

truncation_index = next(
    name for columns, unique, name in table_index_coverages(calls_connection, "calls")
    if columns == ("finish", "ts") and not unique
)
truncation_plan = plan_details(
    calls_connection,
    "SELECT model,COUNT(*) FROM calls WHERE finish IN ('max_tokens','length') "
    "AND ts>=? GROUP BY model",
    ("2026-10-27T00:00:00+00:00",),
)
record_script_check(failures, "recent truncation rollup is an index SEARCH",
                    plan_searches_with(truncation_plan, truncation_index))

reconcile_index = next(
    name for columns, unique, name in table_index_coverages(calls_connection, "calls")
    if columns == ("kind", "provider", "model", "intent") and not unique
)
reconcile_plan = plan_details(
    calls_connection,
    "SELECT id FROM calls WHERE kind='batch' AND provider=? AND model=? AND intent IS ? "
    "ORDER BY rowid DESC LIMIT 1",
    ("provider-1", "model-1", "intent-1"),
)
record_script_check(failures, "calls batch reconciliation is an index SEARCH", plan_searches_with(reconcile_plan, reconcile_index))

retention_connection = sqlite3.connect(":memory:")
content_retention._ensure_content_retention_schema(retention_connection)
retention_connection.executemany(
    "INSERT INTO call_content(id,call_id,ts,prompt_json,system_json,response_json) "
    "VALUES(?,?,?,?,?,?)",
    (
        (
            str(row_number),
            f"call-{row_number % 1_000}",
            f"2026-10-{(row_number % 28) + 1:02d}T00:00:00+00:00",
            "{}",
            "{}",
            "{}",
        )
        for row_number in range(_REPRESENTATIVE_ROWS)
    ),
)
retention_connection.execute("ANALYZE")
retention_index = next(
    name for columns, unique, name in table_index_coverages(retention_connection, "call_content")
    if columns == ("ts",) and not unique
)
retention_plan = plan_details(
    retention_connection,
    "SELECT rowid FROM call_content WHERE ts < ? LIMIT ?",
    ("2026-10-01T00:00:00+00:00", content_retention.CONTENT_RETENTION_PURGE_BATCH_ROWS),
)
record_script_check(failures, "content TTL purge subselect is an index SEARCH", plan_searches_with(retention_plan, retention_index))

ledger_directory = tempfile.mkdtemp(prefix="spendguard-ledger-plan-")
from spendguard import config
ledger = SpendLedger(db_path=os.path.join(ledger_directory, "spend.db"),
                     owned_connection=config.open_ledger_connection(os.path.join(ledger_directory, "spend.db")))
ledger._conn.executemany(
    "INSERT INTO spend_events(id,occurred_at,source,cost_type,cost_basis,provider,model,intent) "
    "VALUES(?,?,?,?,?,?,?,?)",
    (
        (
            str(row_number),
            f"2026-10-{(row_number % 28) + 1:02d}T00:00:00+00:00",
            "gate",
            "batch" if row_number % 2 else "realtime",
            "estimate",
            f"provider-{row_number % 5}",
            f"model-{row_number % 11}",
            f"intent-{row_number % 31}",
        )
        for row_number in range(_REPRESENTATIVE_ROWS)
    ),
)
ledger._conn.execute("ANALYZE")
money_columns = ("source", "cost_type", "cost_basis", "provider", "model", "intent")
money_index = next(
    name for columns, unique, name in table_index_coverages(ledger._conn, "spend_events")
    if columns == money_columns and not unique
)
money_plan = plan_details(
    ledger._conn,
    "SELECT id FROM spend_events WHERE source='gate' AND cost_type='batch' "
    "AND cost_basis='estimate' AND provider=? AND model=? AND intent IS ? ORDER BY rowid DESC LIMIT 1",
    ("provider", "model", "intent"),
)
record_script_check(failures, "money batch reconciliation is an index SEARCH", plan_searches_with(money_plan, money_index))

latest_source_index = next(
    name for columns, unique, name in table_index_coverages(ledger._conn, "spend_events")
    if columns == ("source", "occurred_at", "conv_id") and not unique
)
latest_source_plan = plan_details(
    ledger._conn,
    "SELECT conv_id FROM spend_events WHERE source=? "
    "ORDER BY occurred_at DESC,conv_id DESC LIMIT 1",
    ("gate",),
)
record_script_check(failures, "latest source conversation is an index SEARCH",
                    plan_searches_with(latest_source_plan, latest_source_index))

source_conversation_index = next(
    name for columns, unique, name in table_index_coverages(ledger._conn, "spend_events")
    if columns == ("source", "conv_id", "occurred_at") and not unique
)
source_conversation_plan = plan_details(
    ledger._conn,
    "SELECT rowid,model,cache_read_tok FROM spend_events WHERE source=? AND conv_id=? "
    "ORDER BY occurred_at,rowid",
    ("gate", "conversation"),
)
record_script_check(failures, "source conversation stream is an index SEARCH",
                    plan_searches_with(source_conversation_plan, source_conversation_index))
ledger._conn.close()

decisions_connection = sqlite3.connect(":memory:")
guard._ensure_guard_decisions_schema(decisions_connection)
decisions_connection.executemany(
    "INSERT INTO decisions(ts,day,intent,saved_usd) VALUES(?,?,?,?)",
    (
        (
            f"2026-10-{(row_number % 28) + 1:02d}T00:00:00+00:00",
            f"2026-10-{(row_number % 28) + 1:02d}",
            f"intent-{row_number % 31}",
            1.0,
        )
        for row_number in range(_REPRESENTATIVE_ROWS)
    ),
)
decisions_connection.execute("ANALYZE")
decisions_day_time_index = next(
    name for columns, unique, name in table_index_coverages(decisions_connection, "decisions")
    if columns == ("day", "ts") and not unique
)
decisions_detail_plan = plan_details(
    decisions_connection,
    "SELECT ts,intent,saved_usd FROM decisions WHERE day>=? ORDER BY ts DESC",
    ("2026-10-27",),
)
record_script_check(failures, "recent decision detail is an index SEARCH",
                    any("SEARCH decisions USING INDEX" in detail for detail in decisions_detail_plan))
decisions_summary_plan = plan_details(
    decisions_connection,
    guard.DECISIONS_SUMMARY_INTENT_SINCE_SQL,
    ("2026-10-27", "2026-10-28", 20),
)
record_script_check(failures, "recent decision summary is an index SEARCH",
                    any("SEARCH decisions USING" in detail for detail in decisions_summary_plan)
                    and not any(detail.startswith("SCAN decisions") for detail in decisions_summary_plan))
decisions_connection.close()

squat_connection = sqlite3.connect(":memory:")
squat_connection.execute("CREATE TABLE decoy(value TEXT)")
squat_connection.execute("CREATE TABLE live(value TEXT)")
squatted_name = _available_derived_index_name(squat_connection, "live", ("value",), False)
squat_connection.execute(f'CREATE INDEX "{squatted_name}" ON decoy(value)')
live_index = ensure_ordered_index_coverage(squat_connection, "live", ("value",))
record_script_check(failures, "same-named decoy cannot suppress live-table coverage", live_index != squatted_name and any(
    columns == ("value",) for columns, _unique, _name in table_index_coverages(squat_connection, "live")
))

semcache_connection = sqlite3.connect(":memory:")
semcache_connection.execute(
    "CREATE TABLE semcache(id TEXT PRIMARY KEY,ts TEXT,model TEXT,prompt_hash TEXT,prompt TEXT,output TEXT,emb BLOB)"
)
semcache_connection.executemany(
    "INSERT INTO semcache(id,ts,model,prompt_hash,output) VALUES(?,?,?,?,?)",
    (
        ("old", "2026-10-01T00:00:00+00:00", "model", "duplicate", "old-output"),
        ("new", "2026-10-02T00:00:00+00:00", "model", "duplicate", "new-output"),
        ("other", "2026-10-03T00:00:00+00:00", "model", "distinct", "other-output"),
    ),
)
semcache._repair_semcache_unique_key(semcache_connection)
remaining = semcache_connection.execute("SELECT id,output FROM semcache ORDER BY id").fetchall()
record_script_check(failures, "semcache keeps newest duplicate and every non-duplicate", remaining == [
    ("new", "new-output"), ("other", "other-output")
])
record_script_check(failures, "semcache repaired key is UNIQUE", any(
    columns == ("model", "prompt_hash") and unique
    for columns, unique, _name in table_index_coverages(semcache_connection, "semcache")
))

ambiguous_connection = sqlite3.connect(":memory:")
ambiguous_connection.execute(
    "CREATE TABLE semcache(id TEXT PRIMARY KEY,ts TEXT,model TEXT,prompt_hash TEXT,prompt TEXT,output TEXT,emb BLOB)"
)
ambiguous_connection.executemany(
    "INSERT INTO semcache(id,ts,model,prompt_hash,output) VALUES(?,?,?,?,?)",
    (
        ("first", None, "model", "ambiguous", "first-output"),
        ("second", None, "model", "ambiguous", "second-output"),
        ("distinct", None, "model", "distinct", "distinct-output"),
    ),
)
semcache._repair_semcache_unique_key(ambiguous_connection)
tied_remaining = ambiguous_connection.execute("SELECT id,output FROM semcache ORDER BY id").fetchall()
record_script_check(failures, "tied semcache recency keeps greatest id and every non-duplicate", tied_remaining == [
    ("distinct", "distinct-output"), ("second", "second-output")
])
record_script_check(failures, "tied semcache repair rebuilds the UNIQUE key", any(
    columns == ("model", "prompt_hash") and unique
    for columns, unique, _name in table_index_coverages(ambiguous_connection, "semcache")
))

print(("FAIL: " + ", ".join(failures)) if failures else "all hot-index checks passed")
sys.exit(1 if failures else 0)
