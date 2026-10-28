"""Permanent metadata for reconstructing why and how a governed call ran.

Request and response bodies remain solely in the age-bounded ``call_content`` tier.
"""
import datetime
import json
import logging
from decimal import Decimal, localcontext

from .schema_indexes import ensure_ordered_index_coverage
from .money_scale import real_usd_to_nano_usd, nano_usd_to_usd, assert_nano_total_matches


ROUTING_SNAPSHOT_COLUMN = "routing_snapshot_json"
REQUEST_SCHEMA_COLUMN = "request_schema_json"
REQUEST_MAX_TOKENS_COLUMN = "request_max_tokens"
REQUEST_DECOMPOSITION_COLUMN = "request_decomposition_json"
PERMANENT_CALL_AUDIT_COLUMNS = (
    ROUTING_SNAPSHOT_COLUMN,
    REQUEST_SCHEMA_COLUMN,
    REQUEST_MAX_TOKENS_COLUMN,
    REQUEST_DECOMPOSITION_COLUMN,
)
RECONSTRUCTION_ITEM_NAMES = (
    "subject_body",
    "outcome_body",
    "model_execution",
    "routing_decision",
    "cost_latency",
    "attribution",
    "structured_context",
)
DECISIONS_TABLE = "decisions"
DECISIONS_MIGRATION_TABLE = "decisions_with_call_fk"
DECISION_COLUMNS = (
    "ts", "day", "project", "intent", "basis", "requested_model", "requested_effort",
    "chosen_model", "chosen_effort", "counterfactual_usd", "actual_usd", "saved_usd",
    "why", "reason_class", "call_id",
)
DECISION_NANO_COLUMNS = {column: f"{column}_nano" for column in
                         ("counterfactual_usd", "actual_usd", "saved_usd")}
DECISION_STORAGE_COLUMNS = (*DECISION_COLUMNS, *DECISION_NANO_COLUMNS.values())
DIRECT_ROUTING_BASIS = "direct"
DIRECT_ROUTING_REASON = "requested route served without a recorded substitution"
DIRECT_ROUTING_REASON_CLASS = "other"
UNAVAILABLE_CONTENT_STATUS = "ttl_expired_or_not_retained"
ROUTING_SNAPSHOT_FIELDS = (
    "intent", "plan_usage_pct", "on_overage", "lane_health", "candidate_estimates",
    "basis", "reason_class", "named_gaps",
)


def encode_audit_json(value):
    """Losslessly encode structured metadata; bodies are never passed to this function."""
    if value is None:
        return None
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def decode_audit_json(value):
    return json.loads(value) if value is not None else None


def build_routing_snapshot(*, intent, plan_decision=None, best_value_pick=None):
    """Normalize already-held router evidence without performing any ledger or lane query."""
    plan_decision = plan_decision or {}
    risk = plan_decision.get("risk")
    remaining = risk.get("remaining_pct") if isinstance(risk, dict) else None
    plan_usage = (100.0 - float(remaining)) if remaining is not None else None
    candidate_estimates = plan_decision.get("candidate_estimates")
    if candidate_estimates is None and isinstance(best_value_pick, dict):
        candidate_estimates = best_value_pick.get("considered")
    basis = "best-value" if best_value_pick else "plan-admission" if plan_decision else DIRECT_ROUTING_BASIS
    reason_class = plan_decision.get("reason_class") or (
        "best-value" if best_value_pick else DIRECT_ROUTING_REASON_CLASS)
    values = {
        "intent": intent,
        "plan_usage_pct": plan_usage,
        "on_overage": risk.get("paid_overage") if isinstance(risk, dict) else None,
        "lane_health": risk.get("lane_health") if isinstance(risk, dict) else None,
        "candidate_estimates": candidate_estimates,
        "basis": basis,
        "reason_class": reason_class,
    }
    values["named_gaps"] = [field_name for field_name in ROUTING_SNAPSHOT_FIELDS[:-1]
                            if values.get(field_name) is None]
    return values


def _create_decisions_table(connection, table_name):
    connection.execute(
        f"CREATE TABLE IF NOT EXISTS {table_name} ("
        "ts TEXT, day TEXT, project TEXT, intent TEXT, basis TEXT, "
        "requested_model TEXT, requested_effort TEXT, chosen_model TEXT, chosen_effort TEXT, "
        "counterfactual_usd REAL, actual_usd REAL, saved_usd REAL, why TEXT, reason_class TEXT, "
        "call_id TEXT REFERENCES calls(id), "
        + ",".join(f"{column} INTEGER" for column in DECISION_NANO_COLUMNS.values()) + ")"
    )


def _decision_nano_amounts(counterfactual_usd, actual_usd, saved_usd):
    # Fixed writer signatures reject direct nano inputs: mirrors are derived.
    return tuple(None if amount is None else real_usd_to_nano_usd(float(amount))
                 for amount in (counterfactual_usd, actual_usd, saved_usd))


def _backfill_decision_nano_columns(connection, missing_columns):
    conversion_gaps = []
    for raw_column, nano_column in DECISION_NANO_COLUMNS.items():
        for storage_row_id, raw_amount in connection.execute(
                f"SELECT rowid,{raw_column} FROM {DECISIONS_TABLE}"):
            try:
                nano_amount = None if raw_amount is None else real_usd_to_nano_usd(raw_amount)
            except (ValueError, TypeError, OverflowError) as conversion_error:
                conversion_gaps.append((storage_row_id, raw_column, str(conversion_error)))
                continue
            if nano_column in missing_columns:
                connection.execute(f"UPDATE {DECISIONS_TABLE} SET {nano_column}=? WHERE rowid=?",
                                   (nano_amount, storage_row_id))
    if conversion_gaps:
        raise ValueError(f"decision_nano_invalid_amount_gap count={len({gap[0] for gap in conversion_gaps})} "
                         f"amount_count={len(conversion_gaps)} rows={conversion_gaps!r}")
    for raw_column, nano_column in DECISION_NANO_COLUMNS.items():
        nano_total = sum(row[0] for row in connection.execute(
            f"SELECT {nano_column} FROM {DECISIONS_TABLE} WHERE {nano_column} IS NOT NULL"))
        converted_total = sum(real_usd_to_nano_usd(row[0]) for row in connection.execute(
            f"SELECT {raw_column} FROM {DECISIONS_TABLE} WHERE {raw_column} IS NOT NULL"))
        # Integer accumulation followed by Decimal scaling is an exact Decimal sum,
        # independent of ambient Decimal precision and SQLite SUM integer overflow.
        assert_nano_total_matches(nano_total, nano_usd_to_usd(converted_total),
                                  tolerance_usd='0', label=f"decisions.{raw_column}")
        float_total = connection.execute(
            f"SELECT SUM({raw_column}) FROM {DECISIONS_TABLE}").fetchone()[0]
        # Nano is now more precise than the historical binary-float SUM; this delta
        # is informational, never the equality gate above.
        exact_usd = nano_usd_to_usd(nano_total)
        historical_usd = Decimal(str(float_total or 0))
        with localcontext() as delta_context:
            carry_digits = 2
            delta_context.prec = (max(exact_usd.adjusted(), historical_usd.adjusted())
                                  - min(exact_usd.as_tuple().exponent, historical_usd.as_tuple().exponent)
                                  + carry_digits)
            delta = exact_usd - historical_usd
        logging.getLogger(__name__).info("decisions_nano_float_sum_delta column=%s delta_usd=%s", raw_column, delta)


def ensure_decisions_call_fk_schema(connection, *, commit=True):
    """Create or migrate decisions so nullable historical rows coexist with an enforced call FK."""
    connection.execute("SAVEPOINT decision_nano_schema")
    try:
        _initialize_decision_nano_fk_schema(connection)
        connection.execute("RELEASE SAVEPOINT decision_nano_schema")
    except BaseException:
        connection.execute("ROLLBACK TO SAVEPOINT decision_nano_schema")
        connection.execute("RELEASE SAVEPOINT decision_nano_schema")
        raise
    if commit:
        connection.commit()


def _initialize_decision_nano_fk_schema(connection):
    _create_decisions_table(connection, DECISIONS_TABLE)
    existing_columns = {row[1] for row in connection.execute(
        f"PRAGMA table_info({DECISIONS_TABLE})").fetchall()}
    for column_name in ("why", "reason_class", "call_id"):
        if column_name not in existing_columns:
            connection.execute(f"ALTER TABLE {DECISIONS_TABLE} ADD COLUMN {column_name} TEXT")
    foreign_keys = connection.execute(f"PRAGMA foreign_key_list({DECISIONS_TABLE})").fetchall()
    has_call_fk = any(
        row[2] == "calls" and row[3] == "call_id" and row[4] == "id" for row in foreign_keys
    )
    missing_nano_columns = set(DECISION_NANO_COLUMNS.values()) - existing_columns
    if not has_call_fk or missing_nano_columns:
        connection.execute(f"DROP TABLE IF EXISTS {DECISIONS_MIGRATION_TABLE}")
        _create_decisions_table(connection, DECISIONS_MIGRATION_TABLE)
        carried_columns = (*DECISION_COLUMNS, *(column for column in DECISION_NANO_COLUMNS.values()
                                                if column not in missing_nano_columns))
        projection = ",".join(carried_columns)
        connection.execute(
            f"INSERT INTO {DECISIONS_MIGRATION_TABLE} (rowid,{projection}) "
            f"SELECT rowid,{projection} FROM {DECISIONS_TABLE}"
        )
        connection.execute(f"DROP TABLE {DECISIONS_TABLE}")
        connection.execute(
            f"ALTER TABLE {DECISIONS_MIGRATION_TABLE} RENAME TO {DECISIONS_TABLE}"
        )
        _backfill_decision_nano_columns(connection, missing_nano_columns)
    ensure_ordered_index_coverage(connection, DECISIONS_TABLE, ("day",))
    ensure_ordered_index_coverage(connection, DECISIONS_TABLE, ("intent",))
    ensure_ordered_index_coverage(connection, DECISIONS_TABLE, ("call_id",))


def insert_linked_decision(connection, *, call_id, project, intent, basis, requested_model,
                           requested_effort, chosen_model, chosen_effort, counterfactual_usd,
                           actual_usd, saved_usd, why, reason_class, timestamp=None):
    """Insert the audit decision on the caller's existing transaction and connection."""
    recorded_at = timestamp or datetime.datetime.now(datetime.timezone.utc)
    connection.execute(
        f"INSERT INTO {DECISIONS_TABLE} ({','.join(DECISION_STORAGE_COLUMNS)}) "
        f"VALUES ({','.join('?' for column in DECISION_STORAGE_COLUMNS)})",
        (
            recorded_at.isoformat(timespec="seconds"), recorded_at.strftime("%Y-%m-%d"), project,
            intent, basis, requested_model, requested_effort, chosen_model, chosen_effort,
            (float(counterfactual_usd) if counterfactual_usd is not None else None),
            (float(actual_usd) if actual_usd is not None else None),
            (float(saved_usd) if saved_usd is not None else None), why,
            reason_class, call_id,
            *_decision_nano_amounts(counterfactual_usd, actual_usd, saved_usd),
        ),
    )


def _reconstruction_item(value, *, available=True, reason=None):
    return {
        "status": "available" if available else "unavailable",
        "value": value if available else None,
        "reason": reason,
    }


def reconstruct(call_id):
    """Return the seven audit items for one physical call, with explicit availability."""
    from . import calls, content_retention

    with calls.config.ledger_op('calls', calls._ensure_call_observation_schema) as connection:
        call_columns = (
            "id", "ts", "intent", "caller", "provider", "model", "kind", "cost", "latency",
            "effort", "executor", "project", "outcome", "disposition", "provider_error",
            "settlement_complete", "input_cost_floor",
            "requested_model", "served_model", "resolved_lane", "redirect_reason",
            ROUTING_SNAPSHOT_COLUMN, REQUEST_SCHEMA_COLUMN, REQUEST_MAX_TOKENS_COLUMN,
            REQUEST_DECOMPOSITION_COLUMN,
        )
        row = connection.execute(
            f"SELECT {','.join(call_columns)} FROM calls WHERE id=?", (call_id,)
        ).fetchone()
        if row is None:
            raise KeyError(call_id)
        call = dict(zip(call_columns, row))
        ensure_decisions_call_fk_schema(connection)
        decision_rows = connection.execute(
            f"SELECT {','.join(DECISION_COLUMNS)} FROM {DECISIONS_TABLE} WHERE call_id=? ORDER BY ts",
            (call_id,),
        ).fetchall()
        decisions = [dict(zip(DECISION_COLUMNS, decision_row)) for decision_row in decision_rows]
        content_row = None
        try:
            content_row = connection.execute(
                f"SELECT prompt_json,system_json,response_json FROM {content_retention.CONTENT_RETENTION_TABLE} "
                "WHERE call_id=? ORDER BY ts DESC LIMIT 1",
                (call_id,),
            ).fetchone()
        except Exception:
            content_row = None
    content_available = content_row is not None
    prompt_value = decode_audit_json(content_row[0]) if content_available else None
    system_value = decode_audit_json(content_row[1]) if content_available else None
    response_value = decode_audit_json(content_row[2]) if content_available else None
    unavailable_reason = None if content_available else UNAVAILABLE_CONTENT_STATUS
    items = {
        "subject_body": _reconstruction_item(
            {"prompt": prompt_value, "system": system_value},
            available=content_available,
            reason=unavailable_reason,
        ),
        "outcome_body": _reconstruction_item(
            response_value, available=content_available, reason=unavailable_reason),
        "model_execution": _reconstruction_item({
            "provider": call["provider"], "model": call["model"], "served_model": call["served_model"],
            "requested_model": call["requested_model"], "effort": call["effort"],
            "executor": call["executor"], "resolved_lane": call["resolved_lane"],
            "kind": call["kind"], "outcome": call["outcome"], "disposition": call["disposition"],
            "provider_error": call["provider_error"],
        }),
        "routing_decision": _reconstruction_item({
            "snapshot": decode_audit_json(call[ROUTING_SNAPSHOT_COLUMN]),
            "decisions": decisions,
            "redirect_reason": call["redirect_reason"],
        }),
        "cost_latency": _reconstruction_item({
            "cost": call["cost"], "latency": call["latency"],
            "settlement_complete": call["settlement_complete"],
            "input_cost_floor": call["input_cost_floor"],
        }),
        "attribution": _reconstruction_item({
            "intent": call["intent"], "project": call["project"], "caller": call["caller"],
        }),
        "structured_context": _reconstruction_item({
            "request_schema": decode_audit_json(call[REQUEST_SCHEMA_COLUMN]),
            "request_max_tokens": call[REQUEST_MAX_TOKENS_COLUMN],
            "request_decomposition": decode_audit_json(call[REQUEST_DECOMPOSITION_COLUMN]),
        }),
    }
    return {
        "call_id": call_id,
        "items": items,
        "content_retention": {
            "status": "within_ttl" if content_available else UNAVAILABLE_CONTENT_STATUS,
            "permanent_body_tier": False,
        },
    }
