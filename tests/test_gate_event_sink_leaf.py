"""Verify the dependency-free gate event sink preserves work-integrity event behavior."""
import ast
import os
import sys

os.environ.setdefault("SPENDGUARD_NO_AUTOINSTALL", "1")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

import spendguard  # noqa: E402,F401
from spendguard.manage.admission import gate, gate_event_sink, spend_refusal_error  # noqa: E402
from spendguard.generate.work_integrity import work_integrity  # noqa: E402

LEAF_SOURCE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "src", "spendguard", "manage", "admission", "gate_event_sink.py",
)


def _gate_event_sink_leaf_import_targets():
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
    for is_relative, module_name in _gate_event_sink_leaf_import_targets():
        assert not is_relative, f"leaf has a relative import ({module_name!r})"
        assert not module_name.startswith("spendguard"), f"leaf imports spendguard module {module_name!r}"

    assert gate_event_sink._gate_event_log_sink is gate._log

    original_sink = gate_event_sink._gate_event_log_sink
    recorded_events = []

    def record_gate_event(record):
        recorded_events.append(record)

    try:
        gate_event_sink.set_gate_event_sink(record_gate_event)
        test_record = {"kind": "test"}
        gate_event_sink.emit_gate_event(test_record)
        assert recorded_events == [test_record]

        gate_event_sink.set_gate_event_sink(None)
        try:
            gate_event_sink.emit_gate_event({})
        except RuntimeError:
            pass
        else:
            raise AssertionError("emit_gate_event must fail loudly when the sink is unset")
    finally:
        gate_event_sink.set_gate_event_sink(original_sink)

    assert issubclass(work_integrity.WorkIntegrityViolation, spend_refusal_error.SpendGateRefused)
    try:
        raise work_integrity.WorkIntegrityViolation("test violation")
    except spend_refusal_error.SpendGateRefused:
        pass
    assert not hasattr(work_integrity, "gate")

    print("OK: gate_event_sink leaf is pure, wired, delegating, fail-loud, and exception identity is preserved")


def test_gate_event_sink_leaf():
    """pytest entry point — the module's checks also run as a script via main()."""
    main()


if __name__ == "__main__":
    main()
