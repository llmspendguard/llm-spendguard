"""Discoverable documentation must remain derived from current capability contracts."""
from pathlib import Path

import spendguard
from spendguard import mcp_server
from spendguard.capability_documentation import render_capability_reference
from spendguard.capability_registry import CAPABILITIES, CAPABILITY_ROUTING_GUIDANCE

CAPABILITY_REFERENCE_RELATIVE_PATH = Path('docs') / 'CAPABILITY_REFERENCE.md'


def verify_capability_documentation_is_current():
    """Validate the generated artifact and actual installed API/MCP documentation."""
    root = Path(__file__).resolve().parents[1]
    reference = root / CAPABILITY_REFERENCE_RELATIVE_PATH
    expected = render_capability_reference(CAPABILITIES, routing_guidance=CAPABILITY_ROUTING_GUIDANCE)
    assert reference.read_text(encoding='utf-8') == expected, 'Capability reference needs regeneration'
    for capability in CAPABILITIES:
        assert capability.description.strip(), capability.id
        for parameter in capability.parameters:
            assert parameter.help.strip(), (capability.id, parameter.name)
        for name in spendguard.CAPABILITY_API_COVERAGE.get(capability.id, ()):
            wrapper = getattr(spendguard, name)
            documentation = wrapper.__doc__ or ''
            assert capability.description in documentation, name
            for parameter in wrapper.variant.parameters:
                assert parameter.name in documentation and parameter.help in documentation, (name, parameter.name)
        for name in mcp_server.CAPABILITY_MCP_COVERAGE.get(capability.id, ()):
            description, schema, _ = mcp_server._TOOLS[name]
            assert description.strip(), name
            for parameter_name, parameter_schema in schema.get('properties', {}).items():
                assert parameter_name in description, (name, parameter_name)
                assert parameter_schema.get('description', '').strip(), (name, parameter_name)


if __name__ == '__main__':
    verify_capability_documentation_is_current()
    print('Capability reference, API contracts and MCP argument documentation are current')

