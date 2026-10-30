"""Resumable, bounded-memory compression backfill for local retained content."""

from .capability_cli_parsing import add_registered_cli_argument
import argparse

from . import config
from .content_compression import COMPRESSED_TEXT_PREFIX, compress_retained_text


CONTENT_BACKFILL_BATCH_ROWS = 250
CONTENT_BACKFILL_TARGETS = (
    ("call_content", "id", ("prompt_json", "system_json", "response_json")),
    ("call_io", "id", ("prompt", "output", "context", "system", "req_schema")),
    ("seg_attribution", "seg_id", ("prompt",)),
)


def _compress_content_table_batch(connection, table_name, key_column, content_columns, batch_rows):
    predicates = " OR ".join(
        f"(typeof({column})='text' AND {column}!='' AND substr({column},1,?)!=?)"
        for column in content_columns
    )
    parameters = []
    for _column in content_columns:
        parameters.extend((len(COMPRESSED_TEXT_PREFIX), COMPRESSED_TEXT_PREFIX))
    rows = connection.execute(
        f"SELECT {key_column},{','.join(content_columns)} FROM {table_name} "
        f"WHERE {predicates} LIMIT ?",
        (*parameters, batch_rows),
    ).fetchall()
    assignments = ",".join(f"{column}=?" for column in content_columns)
    for row in rows:
        connection.execute(
            f"UPDATE {table_name} SET {assignments} WHERE {key_column}=?",
            tuple(compress_retained_text(value) for value in row[1:]) + (row[0],),
        )
    connection.commit()
    return len(rows)


def compress_retained_content_database(database_path=None, *, batch_rows=CONTENT_BACKFILL_BATCH_ROWS):
    """Compress one bounded batch per table; rerun until every returned count is zero."""
    if batch_rows <= 0:
        raise ValueError("content backfill batch_rows must be positive")
    connection = config.open_ledger_maintenance_connection(
        database_path or config.db_path(), read_only=False)
    try:
        present_tables = {
            row[0] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
        return {
            table_name: _compress_content_table_batch(
                connection, table_name, key_column, content_columns, batch_rows)
            for table_name, key_column, content_columns in CONTENT_BACKFILL_TARGETS
            if table_name in present_tables
        }
    finally:
        connection.close()


def content_compression_backfill_cli(argv=None):
    parser = argparse.ArgumentParser(prog="spendguard compress-content")
    add_registered_cli_argument(parser, 'compress-content/content_compression_backfill.content_compression_backfill_cli/parser/--run')
    add_registered_cli_argument(parser, 'compress-content/content_compression_backfill.content_compression_backfill_cli/parser/--batch-rows', type=int, default=CONTENT_BACKFILL_BATCH_ROWS)
    arguments = parser.parse_args(argv)
    if not arguments.run:
        print("estimate only: no rows changed; pass --run to compress one bounded batch per table")
        return 0
    counts = compress_retained_content_database(batch_rows=arguments.batch_rows)
    print("compressed rows: " + ", ".join(f"{table}={count}" for table, count in sorted(counts.items())))
    return 0
