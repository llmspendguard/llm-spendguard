"""Verify the dependency-free config accessor bridge preserves config-backed resource state."""
import ast
import os
import sys

os.environ.setdefault("SPENDGUARD_NO_AUTOINSTALL", "1")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from _pytest_fixture_bridge import align_test_startup_home  # noqa: E402

align_test_startup_home(prefix=__name__)

import spendguard  # noqa: E402,F401
from spendguard.store.configuration import config, config_accessors  # noqa: E402

LEAF_SOURCE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "src", "spendguard", "store", "configuration", "config_accessors.py",
)


def _config_accessors_leaf_import_targets():
    """Return every leaf import as (is_relative, module_name), parsed from the full source."""
    with open(LEAF_SOURCE_PATH, "r", encoding="utf-8") as leaf_file:
        tree = ast.parse(leaf_file.read(), filename=LEAF_SOURCE_PATH)
    targets = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                targets.append((False, alias.name))
        elif isinstance(node, ast.ImportFrom):
            targets.append((node.level > 0, node.module or ""))
    return targets


def main():
    for is_relative, module_name in _config_accessors_leaf_import_targets():
        assert not is_relative, f"leaf has a relative import ({module_name!r})"
        assert not module_name.startswith("spendguard"), f"leaf imports spendguard module {module_name!r}"

    assert config_accessors._load_persisted_state is config.load_state
    assert config_accessors._save_persisted_state is config.save_state
    assert config_accessors._get_config_value is config._cfg_get
    assert config_accessors._db_path is config.db_path
    assert config_accessors._disk_guard_floor_bytes is config.disk_guard_floor_bytes
    assert config_accessors._ledger_op is config.ledger_op
    assert config_accessors._pooled_ledger_conn is config.pooled_ledger_conn
    assert config_accessors._uid is config.uid
    original_accessors = (
        config_accessors._load_persisted_state,
        config_accessors._save_persisted_state,
        config_accessors._get_config_value,
        config_accessors._db_path,
        config_accessors._disk_guard_floor_bytes,
        config_accessors._ledger_op,
        config_accessors._pooled_ledger_conn,
        config_accessors._uid,
    )

    recorded_calls = []

    def record_load_persisted_state(name, default=None):
        recorded_calls.append(("load", name, default))
        return default

    def record_save_persisted_state(name, obj, loud=True):
        recorded_calls.append(("save", name, obj, loud))
        return True

    def record_get_config_value(section, key, default=None):
        recorded_calls.append(("get", section, key, default))
        return default

    def record_db_path():
        recorded_calls.append(("db_path",))
        return "sentinel-ledger-path"

    def record_disk_guard_floor_bytes():
        recorded_calls.append(("disk_guard_floor_bytes",))
        return 123456

    def record_ledger_op(key, ensure_schema, path=None):
        recorded_calls.append(("ledger_op", key))
        return "stub-cm"

    def record_pooled_ledger_conn(key, ensure_schema, path=None):
        recorded_calls.append(("pooled", key))
        return "stub-conn"

    def record_uid(n=16):
        recorded_calls.append(("uid", n))
        return "stub-uid"

    try:
        config_accessors.set_config_accessors(
            record_load_persisted_state,
            record_save_persisted_state,
            record_get_config_value,
            record_db_path,
            record_disk_guard_floor_bytes,
            record_ledger_op,
            record_pooled_ledger_conn,
            record_uid,
        )
        assert config_accessors.load_persisted_state("x", {}) == {}
        assert config_accessors.save_persisted_state("x", {}, loud=False) is True
        assert config_accessors.get_config_value("a", "b", 1) == 1
        assert config_accessors.get_db_path() == "sentinel-ledger-path"
        assert config_accessors.get_disk_guard_floor_bytes() == 123456
        assert config_accessors.open_ledger_op("learn", lambda c: None) == "stub-cm"
        assert config_accessors.open_pooled_ledger_conn("learn", lambda c: None) == "stub-conn"
        assert config_accessors.new_uid() == "stub-uid"
        assert recorded_calls == [
            ("load", "x", {}),
            ("save", "x", {}, False),
            ("get", "a", "b", 1),
            ("db_path",),
            ("disk_guard_floor_bytes",),
            ("ledger_op", "learn"),
            ("pooled", "learn"),
            ("uid", 16),
        ]

        config_accessors._load_persisted_state = None
        try:
            config_accessors.load_persisted_state("x", {})
        except RuntimeError:
            pass
        else:
            raise AssertionError("load_persisted_state must fail loudly when its accessor is unset")

        config_accessors._db_path = None
        try:
            config_accessors.get_db_path()
        except RuntimeError:
            pass
        else:
            raise AssertionError("get_db_path must fail loudly when its accessor is unset")

        config_accessors._disk_guard_floor_bytes = None
        try:
            config_accessors.get_disk_guard_floor_bytes()
        except RuntimeError:
            pass
        else:
            raise AssertionError("get_disk_guard_floor_bytes must fail loudly when its accessor is unset")

        config_accessors._ledger_op = None
        try:
            config_accessors.open_ledger_op("learn", lambda c: None)
        except RuntimeError:
            pass
        else:
            raise AssertionError("open_ledger_op must fail loudly when its accessor is unset")

        config_accessors._pooled_ledger_conn = None
        try:
            config_accessors.open_pooled_ledger_conn("learn", lambda c: None)
        except RuntimeError:
            pass
        else:
            raise AssertionError("open_pooled_ledger_conn must fail loudly when its accessor is unset")

        config_accessors._uid = None
        try:
            config_accessors.new_uid()
        except RuntimeError:
            pass
        else:
            raise AssertionError("new_uid must fail loudly when its accessor is unset")
    finally:
        config_accessors.set_config_accessors(*original_accessors)

    from spendguard.manage.token_operations import resource_state
    from spendguard.manage.admission import disk_guard
    from spendguard.analyze.learning import learn

    assert not hasattr(resource_state, "config")
    assert not hasattr(disk_guard, "config")
    assert disk_guard.get_disk_guard_floor_bytes is config_accessors.get_disk_guard_floor_bytes
    assert not hasattr(learn, "config")
    assert callable(learn.insights_ledger_op)
    with learn.insights_ledger_op() as handle:
        assert handle.execute("SELECT 1").fetchone()[0] == 1
    resource_key = "lane:_ctx13_test"
    resource_state.cool(resource_key, 1, "test")
    assert resource_state.cooling(resource_key) is True
    resource_state.clear_cooldown(resource_key)

    print("OK: config_accessors leaf is pure, wired, delegating, fail-loud, and resource_state persistence works")


def test_config_accessors_leaf():
    """pytest entry point — the module's checks also run as a script via main()."""
    main()


if __name__ == "__main__":
    main()
