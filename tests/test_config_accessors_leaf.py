"""Verify the dependency-free config accessor bridge preserves config-backed resource state."""
import ast
import os
import sys

os.environ.setdefault("SPENDGUARD_NO_AUTOINSTALL", "1")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

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
    original_accessors = (
        config_accessors._load_persisted_state,
        config_accessors._save_persisted_state,
        config_accessors._get_config_value,
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

    try:
        config_accessors.set_config_accessors(
            record_load_persisted_state,
            record_save_persisted_state,
            record_get_config_value,
        )
        assert config_accessors.load_persisted_state("x", {}) == {}
        assert config_accessors.save_persisted_state("x", {}, loud=False) is True
        assert config_accessors.get_config_value("a", "b", 1) == 1
        assert recorded_calls == [
            ("load", "x", {}),
            ("save", "x", {}, False),
            ("get", "a", "b", 1),
        ]

        config_accessors._load_persisted_state = None
        try:
            config_accessors.load_persisted_state("x", {})
        except RuntimeError:
            pass
        else:
            raise AssertionError("load_persisted_state must fail loudly when its accessor is unset")
    finally:
        config_accessors.set_config_accessors(*original_accessors)

    from spendguard.manage.token_operations import resource_state

    assert not hasattr(resource_state, "config")
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
