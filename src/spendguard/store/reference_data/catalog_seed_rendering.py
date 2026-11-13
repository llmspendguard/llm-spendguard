"""Pure deterministic rendering of authored catalog documents into seed SQL.

The gateway and the offline generator share this renderer. It accepts document
and schema values and performs no filesystem or database access.
"""
import json

MODEL_SCALAR_FIELDS = (
    "id", "provider", "retired_alias_of", "metered_id", "price", "price_error",
    "output_ceiling", "context_window", "as_of", "provider_base", "reasoning",
    "vision", "capabilities", "billing_exempt_reason",
)
MODEL_SOURCE_FIELDS = frozenset((*MODEL_SCALAR_FIELDS, "aliases", "lane_spellings"))
MODEL_OPTIONAL_SOURCE_FIELDS = frozenset(("price_intervals",))
MODEL_OPTIONAL_SCALAR_FIELDS = ("price_intervals",)


def _catalog_seed_sql_literal(value):
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, (dict, list)):
        value = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return "'" + str(value).replace("'", "''") + "'"


def _catalog_seed_insert_statement(table, columns, values):
    return f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({', '.join(map(_catalog_seed_sql_literal, values))});"


def render_catalog_seed(document, schema_text, *, provenance_authored, provenance_sourced):
    """Return deterministic SQL preserving sparse records and section shapes.

    Missing optional values remain missing through ``present_fields``. Unknown
    model fields are rejected instead of being silently lost. An empty document
    is valid for local authoring; the shipped-seed CLI rejects empty catalogs.
    """
    models = document.get("models") or {}
    actual_fields = {key for record in models.values() for key in record}
    unsupported_fields = actual_fields - MODEL_SOURCE_FIELDS - MODEL_OPTIONAL_SOURCE_FIELDS
    if unsupported_fields:
        raise ValueError(f"unsupported model fields: {sorted(unsupported_fields)}")
    statements = [schema_text.rstrip(), "", "BEGIN IMMEDIATE;"]
    scalar_fields = (*MODEL_SCALAR_FIELDS, *MODEL_OPTIONAL_SCALAR_FIELDS)
    emitted_models = 0
    for model_id, record in sorted(models.items()):
        if record.get("id") != model_id:
            raise ValueError(f"catalog key/id mismatch: {model_id!r} != {record.get('id')!r}")
        statements.append(_catalog_seed_insert_statement(
            "models", (*scalar_fields, "present_fields", "provenance"),
            tuple(record.get(field) for field in scalar_fields)
            + (sorted(record), provenance_authored),
        ))
        emitted_models += 1
        for ordinal, alias in enumerate(record.get("aliases") or []):
            statements.append(_catalog_seed_insert_statement(
                "model_aliases", ("model_id", "ordinal", "alias", "provenance"),
                (model_id, ordinal, alias, provenance_authored),
            ))
        for lane, spellings in sorted((record.get("lane_spellings") or {}).items()):
            for ordinal, spelling in enumerate(spellings):
                statements.append(_catalog_seed_insert_statement(
                    "model_lane_spellings", ("model_id", "lane", "ordinal", "spelling", "provenance"),
                    (model_id, lane, ordinal, spelling, provenance_authored),
                ))
    if emitted_models != len(models):
        raise AssertionError(f"emitted {emitted_models} model rows from {len(models)} source records")
    for section, entries in sorted((key, value) for key, value in document.items() if key != "models"):
        section_is_object = isinstance(entries, dict)
        statements.append(_catalog_seed_insert_statement(
            "catalog_section_shapes", ("section", "is_object"), (section, section_is_object),
        ))
        normalized_entries = entries if section_is_object else {"value": entries}
        for entry_key, value in sorted(normalized_entries.items()):
            statements.append(_catalog_seed_insert_statement(
                "catalog_sections", ("section", "entry_key", "value", "provenance"),
                (section, entry_key, json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False),
                 provenance_sourced),
            ))
    statements.extend(("COMMIT;", ""))
    return "\n".join(statements)
