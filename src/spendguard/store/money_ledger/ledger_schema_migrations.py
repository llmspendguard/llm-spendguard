"""Writer-owned nano migration routing and transactional completion records."""
import functools

NANO_SCHEMA_MIGRATION_VERSION = 1
SPEND_NANO_SCHEMA_MIGRATION = "spend_events_nano"
CALL_NANO_SCHEMA_MIGRATION = "calls_nano"


def own_spend_ledger_schema_reads(migration):
    """Use the existing read owner while retaining home-aligned dispatch.

    Native SQLite connections already see their own writes and cannot hold extra
    attributes. Restore the previous owner on success and on failure.
    """
    @functools.wraps(migration)
    def execute_owned_nano_schema_migration(connection, *args, **kwargs):
        if not hasattr(connection, '_ledger_home_path'):
            return migration(connection, *args, **kwargs)
        prior_owner = getattr(connection, '_ledger_owned_read_deadline', False)
        try:
            connection._ledger_owned_read_deadline = True
            return migration(connection, *args, **kwargs)
        finally:
            connection._ledger_owned_read_deadline = prior_owner
    return execute_owned_nano_schema_migration


def nano_schema_migration_completed(connection, migration_name):
    # Event SCHEMA_VERSION is row metadata, not a database migration marker.
    # Separate records matter: calls-only databases have no spend table yet.
    connection.execute("CREATE TABLE IF NOT EXISTS ledger_schema_migrations ("
                       "migration TEXT PRIMARY KEY, version INTEGER NOT NULL)")
    completed = connection.execute(
        "SELECT version FROM ledger_schema_migrations WHERE migration=?", (migration_name,)).fetchone()
    return completed is not None and completed[0] >= NANO_SCHEMA_MIGRATION_VERSION


def record_nano_schema_migration_completion(connection, migration_name):
    connection.execute("INSERT OR REPLACE INTO ledger_schema_migrations(migration,version) VALUES(?,?)",
                       (migration_name, NANO_SCHEMA_MIGRATION_VERSION))
