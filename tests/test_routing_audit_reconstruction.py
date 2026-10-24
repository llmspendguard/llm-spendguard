"""Permanent routing audit metadata reconstructs calls without retaining permanent bodies."""
import datetime
import json
import os
import sqlite3
import sys
import tempfile


if not os.environ.get("SPENDGUARD_TEST_ISOLATED"):
    os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"
    os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="spendguard-routing-audit-")
    os.execv(sys.executable, [sys.executable] + sys.argv)

from spendguard import calls, config, content_retention, routing_audit


TEST_PROVIDER = "fixture-provider"
TEST_MODEL = "fixture-provider:fixture-model"
TEST_LANE = "fixture-lane"
TEST_INTENT = "routing-audit-fixture"
TEST_PROJECT = "routing-audit-project"
TEST_EFFORT = "fixture-effort"
TEST_MAX_TOKENS = 321
TEST_LATENCY_SECONDS = 1.25
TEST_BILLED_COST = 0.01
TEST_ZERO_COST = 0.0
FULL_PAYLOAD_REPEAT_COUNT = 12_000
SQL_READ_PREFIXES = ("SELECT", "PRAGMA")
IN_MEMORY_DATABASE = ":memory:"
BODY_COLUMN_NAMES = frozenset(("prompt_json", "system_json", "response_json"))
SQLITE_COLUMN_NAME_INDEX = 1
PERMANENT_BODY_TIER_KEY = "permanent_body_tier"
LEGACY_TIMESTAMP = "legacy-ts"
LEGACY_DAY = "legacy-day"
LEGACY_BASIS = "legacy-basis"
TEST_SCHEMA = {
    "type": "object",
    "properties": {"answer": {"type": "string"}},
    "required": ["answer"],
    "additionalProperties": False,
}
TEST_DECOMPOSITION = {"strategy": "fixture", "parts": ["first", "second"]}
TEST_SNAPSHOT = {
    "intent": TEST_INTENT,
    "plan_usage_pct": 42.0,
    "on_overage": False,
    "lane_health": {TEST_LANE: {"known": True, "remaining_pct": 58.0}},
    "candidate_estimates": [{"model": TEST_MODEL, "cost": 0.0, "quality": "measured"}],
    "basis": "fixture-routing",
    "reason_class": "other",
    "named_gaps": [],
    "full_payload_marker": "routing-evidence-" * FULL_PAYLOAD_REPEAT_COUNT,
}


def record_fixture_call(*, kind, outcome, disposition, retain_content, cost=0.0, executor=TEST_LANE):
    call_id = config.uid()
    recorded_id = calls.record_call(
        TEST_PROVIDER,
        TEST_MODEL,
        kind,
        cost,
        in_tok=3,
        out_tok=5,
        latency=TEST_LATENCY_SECONDS,
        intent=TEST_INTENT,
        project=TEST_PROJECT,
        effort=TEST_EFFORT,
        executor=executor,
        outcome=outcome,
        disposition=disposition,
        call_id=call_id,
        routing_snapshot=TEST_SNAPSHOT,
        request_schema=TEST_SCHEMA,
        request_max_tokens=TEST_MAX_TOKENS,
        request_decomposition=TEST_DECOMPOSITION,
    )
    assert recorded_id == call_id
    if retain_content:
        content_retention.record_governed_call_content(
            call_id,
            {"prompt": "fixture prompt"},
            {"system": "fixture system"},
            {"text": "fixture response", "parsed": {"answer": "complete"}},
        )
    return call_id


def assert_complete_reconstruction(call_id, *, expect_content, expected_cost, expected_executor):
    reconstructed = routing_audit.reconstruct(call_id)
    assert reconstructed["call_id"] == call_id
    assert tuple(reconstructed["items"]) == routing_audit.RECONSTRUCTION_ITEM_NAMES
    for item_name in routing_audit.RECONSTRUCTION_ITEM_NAMES[2:]:
        assert reconstructed["items"][item_name]["status"] == "available"
    expected_body_status = "available" if expect_content else "unavailable"
    assert reconstructed["items"]["subject_body"]["status"] == expected_body_status
    assert reconstructed["items"]["outcome_body"]["status"] == expected_body_status
    execution_value = reconstructed["items"]["model_execution"]["value"]
    assert execution_value["model"] == TEST_MODEL
    assert execution_value["provider"] == TEST_PROVIDER
    assert execution_value["effort"] == TEST_EFFORT
    assert execution_value["executor"] == expected_executor
    assert reconstructed["items"]["routing_decision"]["value"]["snapshot"] == TEST_SNAPSHOT
    cost_latency_value = reconstructed["items"]["cost_latency"]["value"]
    assert cost_latency_value["latency"] == TEST_LATENCY_SECONDS
    assert cost_latency_value["cost"] == expected_cost
    attribution_value = reconstructed["items"]["attribution"]["value"]
    assert attribution_value["intent"] == TEST_INTENT
    assert attribution_value["project"] == TEST_PROJECT
    assert attribution_value["caller"]
    context_value = reconstructed["items"]["structured_context"]["value"]
    assert context_value == {
        "request_schema": TEST_SCHEMA,
        "request_max_tokens": TEST_MAX_TOKENS,
        "request_decomposition": TEST_DECOMPOSITION,
    }
    content_status = reconstructed["content_retention"]
    assert content_status["status"] == (
        "within_ttl" if expect_content else routing_audit.UNAVAILABLE_CONTENT_STATUS)
    assert content_status[PERMANENT_BODY_TIER_KEY] is False


metered_call_id = record_fixture_call(
    kind="realtime", outcome="ok", disposition="served", retain_content=True,
    cost=TEST_BILLED_COST, executor="api")
subscription_call_id = record_fixture_call(
    kind="subscription", outcome="ok", disposition="served", retain_content=False)
refused_call_id = record_fixture_call(
    kind="realtime", outcome="refused", disposition="failed", retain_content=False)
embedding_call_id = record_fixture_call(
    kind="embedding", outcome="ok", disposition="served", retain_content=False)

assert_complete_reconstruction(
    metered_call_id,
    expect_content=True,
    expected_cost=TEST_BILLED_COST,
    expected_executor="api",
)
assert_complete_reconstruction(
    subscription_call_id,
    expect_content=False,
    expected_cost=TEST_ZERO_COST,
    expected_executor=TEST_LANE,
)
assert_complete_reconstruction(
    refused_call_id,
    expect_content=False,
    expected_cost=TEST_ZERO_COST,
    expected_executor=TEST_LANE,
)
assert_complete_reconstruction(
    embedding_call_id,
    expect_content=False,
    expected_cost=TEST_ZERO_COST,
    expected_executor=TEST_LANE,
)

connection = calls._calls_db()
call_columns = {row[1] for row in connection.execute("PRAGMA table_info(calls)").fetchall()}
assert set(routing_audit.PERMANENT_CALL_AUDIT_COLUMNS).issubset(call_columns)
decision_columns = {row[1] for row in connection.execute("PRAGMA table_info(decisions)").fetchall()}
assert "call_id" in decision_columns
foreign_keys = connection.execute("PRAGMA foreign_key_list(decisions)").fetchall()
assert any(row[2] == "calls" and row[3] == "call_id" and row[4] == "id" for row in foreign_keys)
assert connection.execute(
    "SELECT COUNT(*) FROM decisions WHERE call_id IN (?,?,?,?)",
    (metered_call_id, subscription_call_id, refused_call_id, embedding_call_id),
).fetchone() == (4,)

old_timestamp = (
    datetime.datetime.now(datetime.timezone.utc)
    - datetime.timedelta(days=content_retention.content_retention_days() + 1)
).isoformat(timespec="seconds")
connection.execute(
    f"UPDATE {content_retention.CONTENT_RETENTION_TABLE} SET ts=? WHERE call_id=?",
    (old_timestamp, metered_call_id),
)
connection.commit()
content_retention._purge_expired_content_rows(connection)
expired_reconstruction = routing_audit.reconstruct(metered_call_id)
assert expired_reconstruction["content_retention"]["status"] == routing_audit.UNAVAILABLE_CONTENT_STATUS
assert expired_reconstruction["items"]["routing_decision"]["status"] == "available"
assert expired_reconstruction["items"]["structured_context"]["status"] == "available"

table_names = {
    row[0] for row in connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    ).fetchall()
}
body_bearing_tables = set()
for table_name in table_names:
    quoted_table_name = '"' + table_name.replace('"', '""') + '"'
    table_columns = {
        row[SQLITE_COLUMN_NAME_INDEX]
        for row in connection.execute(f"PRAGMA table_info({quoted_table_name})").fetchall()
    }
    if BODY_COLUMN_NAMES & table_columns:
        body_bearing_tables.add(table_name)
assert body_bearing_tables == {content_retention.CONTENT_RETENTION_TABLE}

encoded_snapshot = connection.execute(
    "SELECT routing_snapshot_json FROM calls WHERE id=?", (metered_call_id,)
).fetchone()[0]
assert json.loads(encoded_snapshot) == TEST_SNAPSHOT

traced_statements = []
connection.set_trace_callback(traced_statements.append)
try:
    traced_call_id = record_fixture_call(
        kind="realtime", outcome="ok", disposition="served", retain_content=False)
finally:
    connection.set_trace_callback(None)
assert traced_call_id
blocking_reads = [
    statement for statement in traced_statements
    if statement.lstrip().upper().startswith(SQL_READ_PREFIXES)
]
assert blocking_reads == [], blocking_reads

migration_connection = sqlite3.connect(IN_MEMORY_DATABASE)
migration_connection.execute("CREATE TABLE calls (id TEXT PRIMARY KEY)")
migration_connection.execute(
    "CREATE TABLE decisions (ts TEXT, day TEXT, project TEXT, intent TEXT, basis TEXT, "
    "requested_model TEXT, requested_effort TEXT, chosen_model TEXT, chosen_effort TEXT, "
    "counterfactual_usd REAL, actual_usd REAL, saved_usd REAL)"
)
migration_connection.execute(
    "INSERT INTO decisions VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
    (LEGACY_TIMESTAMP, LEGACY_DAY, TEST_PROJECT, TEST_INTENT, LEGACY_BASIS, TEST_MODEL,
     TEST_EFFORT, TEST_MODEL, TEST_EFFORT, 0.0, 0.0, 0.0),
)
routing_audit.ensure_decisions_call_fk_schema(migration_connection)
routing_audit.ensure_decisions_call_fk_schema(migration_connection)
assert migration_connection.execute("SELECT COUNT(*) FROM decisions").fetchone() == (1,)
assert migration_connection.execute(
    "SELECT call_id FROM decisions WHERE basis=?", (LEGACY_BASIS,)
).fetchone() == (None,)
migration_foreign_keys = migration_connection.execute("PRAGMA foreign_key_list(decisions)").fetchall()
assert any(
    row[2] == "calls" and row[3] == "call_id" and row[4] == "id"
    for row in migration_foreign_keys
)
migration_connection.close()

print("OK test_routing_audit_reconstruction")
