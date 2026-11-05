"""Accessor bridge for cyclic consumers that need config state/value operations.

This dependency-free leaf lets consumers such as ``resource_state`` use config's
``load_state``/``save_state``/``_cfg_get`` without importing ``config`` and
re-creating the cycle. Config wires its real functions during its eager import.
"""

_load_persisted_state = None
_save_persisted_state = None
_get_config_value = None


def set_config_accessors(load_state, save_state, cfg_get):
    """Injected once by config at its import with config.load_state / config.save_state / config._cfg_get."""
    global _load_persisted_state, _save_persisted_state, _get_config_value
    _load_persisted_state = load_state
    _save_persisted_state = save_state
    _get_config_value = cfg_get


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
