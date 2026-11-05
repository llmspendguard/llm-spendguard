"""Verify the dependency-free ledger-read accessor bridge preserves realized-route reads."""
import ast
import os
import sys

os.environ.setdefault("SPENDGUARD_NO_AUTOINSTALL", "1")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from _pytest_fixture_bridge import align_test_startup_home  # noqa: E402

align_test_startup_home(prefix=__name__)

import spendguard  # noqa: E402,F401
from spendguard.store.spend_search import ledger_read, ledger_read_accessors  # noqa: E402

LEAF_SOURCE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "src", "spendguard", "store", "spend_search", "ledger_read_accessors.py",
)


def _ledger_read_accessors_leaf_import_targets():
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
    for is_relative, module_name in _ledger_read_accessors_leaf_import_targets():
        assert not is_relative, f"leaf has a relative import ({module_name!r})"
        assert not module_name.startswith("spendguard"), f"leaf imports spendguard module {module_name!r}"

    assert ledger_read_accessors._ledger_table_exists is ledger_read.ledger_read_table_exists
    assert ledger_read_accessors._read_routing_history is ledger_read.read_routing_history_report
    assert ledger_read_accessors._ledger_calls_table == ledger_read.LEDGER_CALLS_TABLE
    original_accessors = (
        ledger_read_accessors._ledger_table_exists,
        ledger_read_accessors._read_routing_history,
        ledger_read_accessors._ledger_calls_table,
    )
    recorded_calls = []

    def record_ledger_table_exists(table_name):
        recorded_calls.append(("table_exists", table_name))
        return True

    def record_routing_history(intent, *, since=None, as_of=None, limit=20):
        recorded_calls.append(("routing_history", intent, since, as_of, limit))
        return {"intent": intent}

    try:
        ledger_read_accessors.set_ledger_read_accessors(
            record_ledger_table_exists,
            record_routing_history,
            "stubbed-calls-table",
        )
        assert ledger_read_accessors.read_ledger_table_exists("calls") is True
        assert ledger_read_accessors.read_routing_history("x", since="s", as_of="a", limit=3) == {"intent": "x"}
        assert ledger_read_accessors.get_ledger_calls_table() == "stubbed-calls-table"
        assert recorded_calls == [
            ("table_exists", "calls"),
            ("routing_history", "x", "s", "a", 3),
        ]

        ledger_read_accessors._ledger_table_exists = None
        try:
            ledger_read_accessors.read_ledger_table_exists("calls")
        except RuntimeError:
            pass
        else:
            raise AssertionError("read_ledger_table_exists must fail loudly when its accessor is unset")
    finally:
        ledger_read_accessors.set_ledger_read_accessors(*original_accessors)

    from spendguard.analyze.measurement import route_realized

    assert not hasattr(route_realized, "config")
    assert not hasattr(route_realized, "ledger_read")
    report = route_realized.route_realized_report("_ctx14_probe_intent")
    assert report["totals"]["jobs"] == 0

    print("OK: ledger_read_accessors leaf is pure, wired, delegating, fail-loud, and route_realized is repointed")


def test_ledger_read_accessors_leaf():
    """pytest entry point — the module's checks also run as a script via main()."""
    main()


if __name__ == "__main__":
    main()
