"""Generated surfaces preserve bound cores, omissions and honest JSON transport."""
from decimal import Decimal
from inspect import signature
from unittest.mock import patch

import spendguard
from spendguard import mcp_server
from spendguard.capability_registry import CAPABILITIES
from spendguard.capability_surface_generation import (
    capability_variant_tool_schema, capture_capability_tool_call,
    invoke_capability_tool_variant, normalize_capability_tool_result,
)

capabilities = {capability.id: capability for capability in CAPABILITIES}
assert spendguard.ask is __import__('spendguard.crossllm', fromlist=['ask']).ask
assert signature(spendguard.ask) == signature(__import__('spendguard.crossllm', fromlist=['ask']).ask)
variant = capabilities['C002'].variants[0]
with patch('spendguard.gate.control_gate_runtime', return_value=0) as core:
    assert invoke_capability_tool_variant(variant, {}) == 0
    core.assert_called_once_with(cmd='status')

assert normalize_capability_tool_result({('provider', 'fingerprint'): Decimal('0.0100')}) == {
    'entries': [{'key': ['provider', 'fingerprint'], 'value': '0.0100'}]}
assert normalize_capability_tool_result({'b', 'a'}) == ['a', 'b']
try:
    normalize_capability_tool_result(object())
except TypeError:
    pass
else:
    raise AssertionError('Opaque values must fail closed')

callback_variant = capabilities['C038'].variants[0]
assert 'parse' not in capability_variant_tool_schema(callback_variant)['properties']
try:
    invoke_capability_tool_variant(callback_variant, {'parse': 'opaque callback'})
except TypeError:
    pass
else:
    raise AssertionError('Python callback controls must be explicitly restricted')

date_variant = capabilities['C110'].variants[0]
from datetime import date
with patch('spendguard.pricing.freshness', return_value={}) as core:
    invoke_capability_tool_variant(date_variant, {'today': date.today().isoformat()})
    assert isinstance(core.call_args.kwargs['today'], date)

def emit_generated_transport_report(**kwargs):
    print('complete report')
    return kwargs

with patch('spendguard.gate.control_gate_runtime', side_effect=emit_generated_transport_report):
    assert capture_capability_tool_call(variant, {}) == {
        'result': {'cmd': 'status'}, 'stdout': 'complete report\n', 'stderr': ''}

for capability in CAPABILITIES:
    for surface in capability.surfaces:
        assert surface.present or surface.absence_classification == 'DELIBERATE'
    if next(surface for surface in capability.surfaces if surface.surface == 'API').present:
        assert spendguard.CAPABILITY_API_COVERAGE[capability.id]
    if next(surface for surface in capability.surfaces if surface.surface == 'MCP').present:
        assert mcp_server.CAPABILITY_MCP_COVERAGE[capability.id]

print('generated callable contracts: bound cores, omissions, identity and transport verified')
