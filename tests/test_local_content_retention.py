"""Default-on, complete, local-only governed-call content retention. Offline and hermetic."""
import datetime
import json
import os
import sqlite3
import sys
import tempfile


if not os.environ.get("SPENDGUARD_TEST_ISOLATED"):
    os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"
    os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="spendguard-content-retention-")
    os.execv(sys.executable, [sys.executable] + sys.argv)

from spendguard import adapters, callio, calls, config, content_retention, saas, truth


failures = []
TEST_MODEL = "openai:gpt-5.5"


def check(name, condition):
    print(("  [OK] " if condition else "  [FAIL] ") + name)
    if not condition:
        failures.append(name)


legacy_cap = callio.snip_chars()
long_prompt = "prompt:" + ("P" * (legacy_cap * 3))
long_system = "system:" + ("S" * (legacy_cap * 2))
long_response = "response:" + ("R" * (legacy_cap * 4))
original_guarded = adapters._call_guarded


def governed_stub(model, prompt, **kwargs):
    call_id = calls.record_call("fixture-provider", model, "subscription", 0.0,
                                in_tok=1, out_tok=1, executor="fixture-executor")
    return {"text": long_response, "provider": "fixture-provider", "model": model,
            "in_tok": 1, "out_tok": 1, "cost": 0.0, "executor": "fixture-executor",
            "tool_calls": [{"name": "fixture-tool", "arguments": {"complete": True}}],
            "call_id": call_id, "error": None}


adapters._call_guarded = governed_stub
try:
    result = adapters.call(TEST_MODEL, long_prompt, system=long_system, sig="retention-fixture")
finally:
    adapters._call_guarded = original_guarded

connection = content_retention._content_retention_db()
row = connection.execute(
    f"SELECT call_id,prompt_json,system_json,response_json FROM {content_retention.CONTENT_RETENTION_TABLE}"
).fetchone()
check("default-on governed call writes one content row", row is not None)
check("content row joins the existing calls metadata row", row and row[0] == result["call_id"] and
      calls._calls_db().execute("SELECT 1 FROM calls WHERE id=?", (row[0],)).fetchone() == (1,))
check("prompt round-trips whole beyond the legacy cap", row and json.loads(row[1]) == long_prompt and
      len(json.loads(row[1])) == len(long_prompt))
check("system round-trips whole", row and json.loads(row[2]) == long_system)
decoded_response = json.loads(row[3]) if row else {}
check("response text and structured/tool output round-trip whole", decoded_response.get("text") == long_response and
      decoded_response.get("tool_calls") == result["tool_calls"])

retention_days = content_retention.content_retention_days()
now = datetime.datetime.now(datetime.timezone.utc)
old_timestamp = (now - datetime.timedelta(days=retention_days + 1)).isoformat(timespec="seconds")
fresh_timestamp = (now - datetime.timedelta(days=retention_days - 1)).isoformat(timespec="seconds")
content_retention.record_governed_call_content("old-call", "old-secret", None, {"text": "old-secret"},
                                               timestamp=old_timestamp)
content_retention.record_governed_call_content("fresh-call", "fresh-secret", None, {"text": "fresh-secret"},
                                               timestamp=fresh_timestamp)
content_retention._purge_expired_content_rows(connection, now=now)
remaining = {item[0] for item in connection.execute(
    f"SELECT call_id FROM {content_retention.CONTENT_RETENTION_TABLE}").fetchall()}
check("TTL purge removes old rows and retains in-window rows", "old-call" not in remaining and "fresh-call" in remaining)

connection.execute(f"CREATE TRIGGER content_retention_purge_failure BEFORE DELETE ON "
                   f"{content_retention.CONTENT_RETENTION_TABLE} BEGIN SELECT RAISE(ABORT,'fixture'); END")
connection.execute(
    f"INSERT INTO {content_retention.CONTENT_RETENTION_TABLE} "
    "(id,call_id,ts,prompt_json,system_json,response_json) VALUES (?,?,?,?,?,?)",
    ("failure-row", "failure-call", old_timestamp, '"failure-secret"', "null", '{}'))
connection.commit()
purge_failure_propagated = False
try:
    content_retention._purge_expired_content_rows(connection, now=now)
except sqlite3.DatabaseError:
    pass
try:
    content_retention._ensure_content_retention_schema(connection)
except sqlite3.DatabaseError:
    purge_failure_propagated = True
failure_row_still_present = connection.execute(
    f"SELECT 1 FROM {content_retention.CONTENT_RETENTION_TABLE} WHERE id=?", ("failure-row",)).fetchone()
check("failed purge rolls back without over-delete or propagation through the open seam",
      failure_row_still_present == (1,) and not purge_failure_propagated)
connection.execute("DROP TRIGGER content_retention_purge_failure")
connection.commit()

original_content_db = content_retention._content_retention_db
content_retention._content_retention_db = lambda: (_ for _ in ()).throw(sqlite3.OperationalError("fixture"))
try:
    write_failure_result = content_retention.record_governed_call_content(
        "failure-call", "never-logged", None, {"text": "never-logged"})
finally:
    content_retention._content_retention_db = original_content_db
check("retention write/purge infrastructure failure does not propagate to call path", write_failure_result is None)

original_cfg_get = config._cfg_get


def disabled_config(section, key, default=None):
    if section == content_retention.CONTENT_RETENTION_CONFIG_SECTION and key == content_retention.CONTENT_RETENTION_ENABLED_KEY:
        return False
    return original_cfg_get(section, key, default)


config._cfg_get = disabled_config
try:
    before_opt_out = connection.execute(
        f"SELECT COUNT(*) FROM {content_retention.CONTENT_RETENTION_TABLE}").fetchone()[0]
    opt_out_result = content_retention.record_governed_call_content(
        "opted-out", "opted-out-secret", None, {"text": "opted-out-secret"})
    after_opt_out = connection.execute(
        f"SELECT COUNT(*) FROM {content_retention.CONTENT_RETENTION_TABLE}").fetchone()[0]
finally:
    config._cfg_get = original_cfg_get
check("content_retention.enabled=False opts out while default remains on",
      opt_out_result is None and before_opt_out == after_opt_out)

secret_markers = (long_prompt, long_system, long_response, "fresh-secret", "failure-secret")
original_saas_connection = saas.saas_connection
original_rollup_rows = saas._rollup_rows
original_guarded_rows = saas._guarded_rows
original_contributor_ok = saas.contributor_ok
saas.saas_connection = lambda: {"enabled": True, "visibility": "org"}
saas._rollup_rows = lambda since=None: []
saas._guarded_rows = lambda since=None: []
saas.contributor_ok = lambda: (True, "fixture")
try:
    saas_payload = saas.push_rollup(dry=True)
finally:
    saas.saas_connection = original_saas_connection
    saas._rollup_rows = original_rollup_rows
    saas._guarded_rows = original_guarded_rows
    saas.contributor_ok = original_contributor_ok

original_truth_connection = saas.saas_connection
original_owner_ok = __import__("spendguard.reconcile", fromlist=["owner_ok"]).owner_ok
reconcile_module = __import__("spendguard.reconcile", fromlist=["owner_ok"])
saas.saas_connection = lambda: {"enabled": True, "visibility": "org", "owns_account": True}
reconcile_module.owner_ok = lambda connection: (True, "fixture")
try:
    truth_payload = truth.push_truth(dry=True, truth_rows=[])
finally:
    saas.saas_connection = original_truth_connection
    reconcile_module.owner_ok = original_owner_ok

serialized_pushes = json.dumps({"saas": saas_payload, "truth": truth_payload}, sort_keys=True)
check("real SaaS/truth payload serializers contain zero retained content/table fields",
      content_retention.CONTENT_RETENTION_TABLE not in serialized_pushes and
      "prompt_json" not in serialized_pushes and "response_json" not in serialized_pushes and
      all(marker not in serialized_pushes for marker in secret_markers))
check("existing forensic call metadata remains recorded", calls._calls_db().execute(
    "SELECT intent,provider,model,executor FROM calls WHERE id=?", (result["call_id"],)).fetchone() ==
      ("retention-fixture", "fixture-provider", TEST_MODEL, "fixture-executor"))

print(f"\n{'[FAIL]' if failures else 'OK'} test_local_content_retention: {len(failures)} failure(s)")
sys.exit(1 if failures else 0)
