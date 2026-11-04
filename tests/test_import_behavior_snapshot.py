"""Golden import-order equivalence and mutation proofs for the spend gate."""

import copy
from pathlib import Path

from spendguard.import_behavior_snapshot import (
    IMPORT_BEHAVIOR_ENTRY_POINTS,
    capture_import_behavior_snapshot,
    import_behavior_entries_match,
    import_behavior_sdk_invariant_holds,
    load_import_behavior_snapshot,
)


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
UNGATED_MUTATION_PROVIDER_METHOD = "openai.resources.files.Files.create"
UNGATED_MUTATION_STATE = "ungated"
NON_ALLOWLISTED_EXECUTED_MODULE = "spendguard.unreviewed_migration_scaffold"


def test_every_import_entry_point_matches_golden_and_gates_sdk_methods():
    golden = load_import_behavior_snapshot(REPOSITORY_ROOT)
    actual_entries = capture_import_behavior_snapshot(REPOSITORY_ROOT)
    assert tuple(golden["entry_points"]) == IMPORT_BEHAVIOR_ENTRY_POINTS
    for entry_point in IMPORT_BEHAVIOR_ENTRY_POINTS:
        expected = golden["entry_points"][entry_point]
        actual = actual_entries[entry_point]
        assert import_behavior_entries_match(
            expected,
            actual,
            golden["name_normalization"],
            golden["migration_scaffold_executed_module_allowlist"],
        ), entry_point
        assert import_behavior_sdk_invariant_holds(actual), entry_point


def test_order_change_and_ungated_sdk_mutations_are_independently_rejected():
    golden = load_import_behavior_snapshot(REPOSITORY_ROOT)
    entry_point = IMPORT_BEHAVIOR_ENTRY_POINTS[0]
    expected = golden["entry_points"][entry_point]

    reordered = copy.deepcopy(expected)
    modules = reordered["executed_modules"]
    reordered["executed_modules"] = list(reversed(modules))
    allowlist = golden["migration_scaffold_executed_module_allowlist"]
    assert not import_behavior_entries_match(expected, reordered, golden["name_normalization"], allowlist)
    assert import_behavior_sdk_invariant_holds(reordered)

    ungated = copy.deepcopy(expected)
    ungated["sdk_methods"][UNGATED_MUTATION_PROVIDER_METHOD] = UNGATED_MUTATION_STATE
    assert import_behavior_entries_match(expected, ungated, golden["name_normalization"], allowlist) is False
    assert import_behavior_sdk_invariant_holds(ungated) is False

    unreviewed_scaffold = copy.deepcopy(expected)
    unreviewed_scaffold["executed_modules"].append(NON_ALLOWLISTED_EXECUTED_MODULE)
    assert not import_behavior_entries_match(
        expected, unreviewed_scaffold, golden["name_normalization"], allowlist,
    )


test_every_import_entry_point_matches_golden_and_gates_sdk_methods()
test_order_change_and_ungated_sdk_mutations_are_independently_rejected()
print("[OK] import behavior golden matches and both independent regression mutations are rejected")
