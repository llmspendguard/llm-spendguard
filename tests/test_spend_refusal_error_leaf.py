"""Verify SpendGateRefused's relocation to the dependency-free leaf is identity-preserving and keeps the leaf a sink.

ctx#11 moved the deliberate-refusal ROOT exception SpendGateRefused out of the cycle-bound admission gate into the
leaf module manage.admission.spend_refusal_error, and gate re-exports it. This test proves four things that must all
hold for the move to be behavior-neutral:

  * LEAF PURITY — the leaf imports ONLY the standard library (no spendguard / no relative import). A spendguard import
    here would re-create the very job_fingerprint→gate cycle the move exists to cut, so this guards the invariant
    structurally (AST of the leaf's own source, not a name heuristic).
  * RE-EXPORT IDENTITY — gate.SpendGateRefused, the legacy `spendguard.gate` shim path, and the leaf all name the
    SAME class object. There is exactly one SpendGateRefused.
  * SUBCLASS PROPAGATION — the gate's refusal subclasses and job_fingerprint's refusal subclasses still subclass the
    leaf's SpendGateRefused, so every `except SpendGateRefused` site and the deliberate-stop machinery are unchanged.
  * ROOT TYPE — SpendGateRefused is a RuntimeError subclass (its propagation contract).
"""
import ast
import os
import sys

os.environ.setdefault("SPENDGUARD_NO_AUTOINSTALL", "1")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

import spendguard  # noqa: E402,F401
from spendguard.manage.admission import gate, spend_refusal_error  # noqa: E402
from spendguard.generate.work_integrity import job_fingerprint  # noqa: E402

LEAF_SOURCE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "src", "spendguard", "manage", "admission", "spend_refusal_error.py",
)


def _leaf_import_targets():
    """Every module the leaf imports, read structurally from its own AST (parsing, not meaning).

    Returns a list of (is_relative, module_name) for each import statement in the leaf. A pure-stdlib leaf has NONE.
    """
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
    # LEAF PURITY: no relative import, and no import that reaches into spendguard — the leaf must stay a sink.
    for is_relative, module_name in _leaf_import_targets():
        assert not is_relative, f"leaf has a relative import ({module_name!r}) — it must be a dependency-free sink"
        assert not module_name.startswith("spendguard"), (
            f"leaf imports spendguard module {module_name!r} — that re-creates the cycle the move exists to cut")

    # RE-EXPORT IDENTITY: one class object, named from the leaf, re-exported by gate, reachable via the legacy shim.
    from spendguard.gate import SpendGateRefused as legacy_shim_spend_gate_refused  # noqa: E402
    assert gate.SpendGateRefused is spend_refusal_error.SpendGateRefused
    assert legacy_shim_spend_gate_refused is spend_refusal_error.SpendGateRefused

    # ROOT TYPE: still a RuntimeError subclass (its propagation contract).
    assert issubclass(spend_refusal_error.SpendGateRefused, RuntimeError)

    # SUBCLASS PROPAGATION: gate's refusal subclasses and job_fingerprint's still subclass the leaf's root.
    assert issubclass(gate.MeteredCallRefused, spend_refusal_error.SpendGateRefused)
    assert issubclass(gate.EffortNotHonored, spend_refusal_error.SpendGateRefused)
    assert issubclass(job_fingerprint.JobPlanDrift, spend_refusal_error.SpendGateRefused)
    assert issubclass(job_fingerprint.JobMethodUndecided, spend_refusal_error.SpendGateRefused)

    print("OK: spend_refusal_error leaf is stdlib-only, SpendGateRefused identity + subclass propagation preserved")


def test_spend_refusal_error_leaf():
    """pytest entry point — the module's checks also run as a script via main()."""
    main()


if __name__ == "__main__":
    main()
