"""Regenerate the discoverable capability reference (docs/CAPABILITY_REFERENCE.md) from the registry SSOT.

The reference is a GENERATED artifact: tests/test_capability_documentation_guard.py asserts the committed file
equals render_capability_reference(CAPABILITIES, routing_guidance=CAPABILITY_ROUTING_GUIDANCE). Any edit to the
capability registry (src/spendguard/interface/capability_registry_data.jsonl) changes that rendering, so the doc
must be regenerated from the same renderer the guard uses — never hand-edited. Run after any registry change.
"""
from pathlib import Path

from spendguard.interface.capability_documentation import render_capability_reference
from spendguard.interface.capability_registry import CAPABILITIES, CAPABILITY_ROUTING_GUIDANCE

CAPABILITY_REFERENCE_RELATIVE_PATH = Path("docs") / "CAPABILITY_REFERENCE.md"


def main():
    repository_root = Path(__file__).resolve().parents[2]
    reference = repository_root / CAPABILITY_REFERENCE_RELATIVE_PATH
    rendered = render_capability_reference(CAPABILITIES, routing_guidance=CAPABILITY_ROUTING_GUIDANCE)
    reference.write_text(rendered, encoding="utf-8")
    print(f"Capability reference regenerated: {CAPABILITY_REFERENCE_RELATIVE_PATH} ({len(rendered)} chars)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
