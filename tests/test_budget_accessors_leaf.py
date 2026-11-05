"""Verify the dependency-free budget accessor bridge preserves attribution tags."""
import ast
import os
import sys

os.environ.setdefault("SPENDGUARD_NO_AUTOINSTALL", "1")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from _pytest_fixture_bridge import align_test_startup_home  # noqa: E402

align_test_startup_home(prefix=__name__)

import spendguard  # noqa: E402,F401
from spendguard.manage.admission import budget, budget_accessors  # noqa: E402
from spendguard.store.configuration import config_accessors  # noqa: E402

LEAF_SOURCE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "src", "spendguard", "manage", "admission", "budget_accessors.py",
)


def _budget_accessors_leaf_import_targets():
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
    for is_relative, module_name in _budget_accessors_leaf_import_targets():
        assert not is_relative, f"leaf has a relative import ({module_name!r})"
        assert not module_name.startswith("spendguard"), f"leaf imports spendguard module {module_name!r}"

    assert budget_accessors._conv_getter is not None
    assert budget_accessors._project_getter is not None
    assert callable(budget_accessors._conv_getter)
    assert callable(budget_accessors._project_getter)
    assert isinstance(budget_accessors.conv_id(), str)
    assert isinstance(budget_accessors.project_tag(), str)
    assert budget_accessors.conv_id() == budget._conv()
    assert budget_accessors.project_tag() == budget._project()

    original_accessors = (budget_accessors._conv_getter, budget_accessors._project_getter)
    try:
        budget_accessors.set_budget_accessors(lambda: "stub-conv", lambda: "stub-proj")
        assert budget_accessors.conv_id() == "stub-conv"
        assert budget_accessors.project_tag() == "stub-proj"

        budget_accessors._conv_getter = None
        try:
            budget_accessors.conv_id()
        except RuntimeError:
            pass
        else:
            raise AssertionError("conv_id must fail loudly when its accessor is unset")

        budget_accessors._project_getter = None
        try:
            budget_accessors.project_tag()
        except RuntimeError:
            pass
        else:
            raise AssertionError("project_tag must fail loudly when its accessor is unset")
    finally:
        budget_accessors.set_budget_accessors(*original_accessors)

    from spendguard.egress import emit

    assert not hasattr(emit, "config")
    assert not hasattr(emit, "budget")
    assert not hasattr(emit, "_budget")
    assert emit.home_dir is config_accessors.home_dir
    assert emit.open_ssl_context is config_accessors.open_ssl_context
    assert emit.conv_id is budget_accessors.conv_id
    assert emit.project_tag is budget_accessors.project_tag

    print("OK: budget_accessors leaf is pure, wired, delegating, fail-loud, and emit is repointed")


def test_budget_accessors_leaf():
    """pytest entry point — the module's checks also run as a script via main()."""
    main()


if __name__ == "__main__":
    main()
