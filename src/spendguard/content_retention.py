"""Machine-local, age-bounded retention of complete governed-call inputs and outputs.

This table is deliberately independent of ``call_io``: that table is an opt-in,
size-capped replay sample, while this store is default-on, complete, and bounded by age.
No SaaS serializer imports this module or selects this table.
"""
import datetime
import json
import logging
import threading

from . import config
from .ledger_fork_locks import new_fork_reset_ledger_state
from .config_schema import SETTINGS
from .schema_indexes import ensure_ordered_index_coverage


CONTENT_RETENTION_CONFIG_SECTION = "content_retention"
CONTENT_RETENTION_ENABLED_KEY = "enabled"
CONTENT_RETENTION_DAYS_KEY = "retention_days"
CONTENT_RETENTION_TABLE = "call_content"
CONTENT_RETENTION_PURGE_BATCH_ROWS = 1000
CONTENT_RETENTION_PURGE_SAVEPOINT = "content_retention_purge"

_content_retention_lock = new_fork_reset_ledger_state(globals(), "_content_retention_lock", threading.RLock)
_content_retention_logger = logging.getLogger(__name__)


def _content_retention_setting_default(key):
    """Read this feature's declared default from the config schema SSOT."""
    return next(
        setting["default"] for setting in SETTINGS
        if setting["section"] == CONTENT_RETENTION_CONFIG_SECTION and setting["key"] == key
    )


def content_retention_enabled():
    """Return the config-SSOT switch; unknown values fail closed to disabled."""
    try:
        return config.is_explicit_true(config._cfg_get(
            CONTENT_RETENTION_CONFIG_SECTION, CONTENT_RETENTION_ENABLED_KEY,
            _content_retention_setting_default(CONTENT_RETENTION_ENABLED_KEY)))
    except Exception:
        return False


def content_retention_days():
    """Return the positive age limit from the config SSOT."""
    value = int(config._cfg_get(
        CONTENT_RETENTION_CONFIG_SECTION, CONTENT_RETENTION_DAYS_KEY,
        _content_retention_setting_default(CONTENT_RETENTION_DAYS_KEY)))
    if value <= 0:
        raise ValueError("content retention days must be positive")
    return value


def _encode_complete_value(value):
    """Losslessly encode supported governed-call structures; never slice evidence."""
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _purge_expired_content_rows(connection, *, now=None):
    """Delete one bounded batch atomically; roll back the entire batch on any failure."""
    current = now or datetime.datetime.now(datetime.timezone.utc)
    cutoff = current - datetime.timedelta(days=content_retention_days())
    connection.execute(f"SAVEPOINT {CONTENT_RETENTION_PURGE_SAVEPOINT}")
    try:
        cursor = connection.execute(
            f"DELETE FROM {CONTENT_RETENTION_TABLE} WHERE rowid IN ("
            f"SELECT rowid FROM {CONTENT_RETENTION_TABLE} WHERE ts < ? LIMIT ?)",
            (cutoff.isoformat(timespec="seconds"), CONTENT_RETENTION_PURGE_BATCH_ROWS),
        )
        connection.execute(f"RELEASE SAVEPOINT {CONTENT_RETENTION_PURGE_SAVEPOINT}")
        connection.commit()
        return cursor.rowcount
    except Exception:
        connection.execute(f"ROLLBACK TO SAVEPOINT {CONTENT_RETENTION_PURGE_SAVEPOINT}")
        connection.execute(f"RELEASE SAVEPOINT {CONTENT_RETENTION_PURGE_SAVEPOINT}")
        connection.rollback()
        raise


def _ensure_content_retention_schema(connection):
    """Apply the additive schema upgrade and attempt an atomic bounded TTL purge."""
    connection.execute(f"""CREATE TABLE IF NOT EXISTS {CONTENT_RETENTION_TABLE}(
        id TEXT PRIMARY KEY,
        call_id TEXT NOT NULL,
        ts TEXT NOT NULL,
        prompt_json TEXT NOT NULL,
        system_json TEXT NOT NULL,
        response_json TEXT NOT NULL
    )""")
    ensure_ordered_index_coverage(connection, CONTENT_RETENTION_TABLE, ("ts",))
    connection.commit()
    try:
        _purge_expired_content_rows(connection)
    except Exception:
        _content_retention_logger.exception(
            "local content TTL purge failed; transaction rolled back without deleting rows")


def _content_retention_db():
    """Open an independently closeable connection with the retention schema ready."""
    return config.fresh_ledger_conn(_ensure_content_retention_schema, config.db_path())


def record_governed_call_content(call_id, prompt, system, response, *, timestamp=None):
    """Persist complete call content locally. Failure is metadata-only and never escapes."""
    if not content_retention_enabled():
        return None
    content_id = config.uid()
    retained_at = timestamp or datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
    connection = None
    try:
        encoded_prompt = _encode_complete_value(prompt)
        encoded_system = _encode_complete_value(system)
        encoded_response = _encode_complete_value(response)
        # Schema setup may synchronously dispatch to the writer; admit it before
        # taking the retention lock, following the global writer lock order.
        connection = _content_retention_db()
        with _content_retention_lock:
            connection.execute(
                f"INSERT INTO {CONTENT_RETENTION_TABLE} "
                "(id,call_id,ts,prompt_json,system_json,response_json) VALUES (?,?,?,?,?,?)",
                (content_id, call_id, retained_at, encoded_prompt, encoded_system, encoded_response),
            )
            connection.commit()
        return content_id
    except (Exception, config.LedgerConnectionLimitError) as exc:
        if connection is not None:
            try:
                connection.rollback()
            except Exception:
                pass
        _content_retention_logger.error(
            "governed-call content retention failed for call_id=%r error_type=%s",
            call_id, type(exc).__name__, exc_info=True)
        return None
    finally:
        if connection is not None:
            try:
                connection.close()
            except Exception:
                pass
