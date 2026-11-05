"""Accessor bridge for cyclic consumers that need config state/value operations.

This dependency-free leaf lets consumers such as ``resource_state`` use config's
``load_state``/``save_state``/``_cfg_get`` without importing ``config`` and
re-creating the cycle. Config wires its real functions during its eager import.
"""

_load_persisted_state = None
_save_persisted_state = None
_get_config_value = None
_db_path = None
_disk_guard_floor_bytes = None
_ledger_op = None
_pooled_ledger_conn = None
_uid = None


def set_config_accessors(load_state, save_state, cfg_get, db_path, disk_guard_floor_bytes,
                         ledger_op, pooled_ledger_conn, uid):
    """Injected by config with the existing five functions plus config.ledger_op,
    config.pooled_ledger_conn, and config.uid."""
    global _load_persisted_state, _save_persisted_state, _get_config_value, _db_path, _disk_guard_floor_bytes
    global _ledger_op, _pooled_ledger_conn, _uid
    _load_persisted_state = load_state
    _save_persisted_state = save_state
    _get_config_value = cfg_get
    _db_path = db_path
    _disk_guard_floor_bytes = disk_guard_floor_bytes
    _ledger_op = ledger_op
    _pooled_ledger_conn = pooled_ledger_conn
    _uid = uid


def load_persisted_state(name, default=None):
    if _load_persisted_state is None:
        raise RuntimeError("import spendguard before loading persisted state so config can wire its accessors")
    return _load_persisted_state(name, default)


def save_persisted_state(name, obj, loud=True):
    if _save_persisted_state is None:
        raise RuntimeError("import spendguard before saving persisted state so config can wire its accessors")
    return _save_persisted_state(name, obj, loud=loud)


def get_config_value(section, key, default=None):
    if _get_config_value is None:
        raise RuntimeError("import spendguard before reading config values so config can wire its accessors")
    return _get_config_value(section, key, default)


def get_db_path():
    if _db_path is None:
        raise RuntimeError("import spendguard before reading the database path so config can wire its accessors")
    return _db_path()


def get_disk_guard_floor_bytes():
    if _disk_guard_floor_bytes is None:
        raise RuntimeError("import spendguard before reading the disk guard floor so config can wire its accessors")
    return _disk_guard_floor_bytes()


def open_ledger_op(key, ensure_schema, path=None):
    if _ledger_op is None:
        raise RuntimeError("import spendguard before opening a ledger operation so config can wire its accessors")
    return _ledger_op(key, ensure_schema, path)


def open_pooled_ledger_conn(key, ensure_schema, path=None):
    if _pooled_ledger_conn is None:
        raise RuntimeError("import spendguard before opening a pooled ledger connection so config can wire its accessors")
    return _pooled_ledger_conn(key, ensure_schema, path)


def new_uid(n=16):
    if _uid is None:
        raise RuntimeError("import spendguard before creating a uid so config can wire its accessors")
    return _uid(n)
