"""The first-phase registry preserves the entire audited baseline without claiming readiness."""
from collections import Counter
from dataclasses import FrozenInstanceError
from importlib.resources import files
import json

from spendguard.capability_registry import CAPABILITIES, CapabilitySchemaNode

AUDIT_CAPABILITY_TOTAL = 278
AUDIT_PRESENT_TOTAL = 517
AUDIT_ABSENT_TOTAL = 317
AUDIT_GAP_TOTAL = 240
AUDIT_DELIBERATE_TOTAL = 77
AUDIT_DOCUMENTED_TOTAL = 55
AUDIT_DOCUMENTATION_STUB_TOTAL = 462
CAPABILITY_STRUCTURAL_TYPES = {"string", "integer", "number", "boolean", "array", "object", "null", "any"}


assert len(CAPABILITIES) == AUDIT_CAPABILITY_TOTAL
assert len({capability.id for capability in CAPABILITIES}) == len(CAPABILITIES)
assert {capability.id for capability in CAPABILITIES} == {
    f"C{index:03d}" for index in range(1, len(CAPABILITIES) + 1)
}
from spendguard.capability_registry import CapabilitySurfaceExposure, CAPABILITY_REGISTRY_RESOURCE
declarations = [json.loads(line) for line in files('spendguard').joinpath(
    CAPABILITY_REGISTRY_RESOURCE).read_text().splitlines()]
exposures = [CapabilitySurfaceExposure(**surface) for declaration in declarations
             for surface in declaration['audit_surfaces']]
assert Counter(surface.present for surface in exposures) == {True: AUDIT_PRESENT_TOTAL, False: AUDIT_ABSENT_TOTAL}
assert Counter(surface.absence_classification for surface in exposures if not surface.present) == {
    "GAP": AUDIT_GAP_TOTAL, "DELIBERATE": AUDIT_DELIBERATE_TOTAL,
}
assert Counter(surface.documentation for surface in exposures if surface.present) == {
    "DOC-OK": AUDIT_DOCUMENTED_TOTAL, "DOC-STUB": AUDIT_DOCUMENTATION_STUB_TOTAL,
}
for capability in CAPABILITIES:
    assert capability.description
    assert {surface.surface for surface in capability.surfaces} == {"CLI", "API", "MCP"}
    for surface in capability.surfaces:
        if surface.present:
            assert surface.names, capability.id
            assert surface.absence_classification is None, capability.id
        else:
            assert surface.absence_classification == "DELIBERATE", capability.id
            assert surface.reason.strip(), capability.id
    assert capability.contract_status != "pending", capability.id
    for parameter in capability.parameters:
        assert parameter.name and parameter.help, capability.id
        assert isinstance(parameter.schema, CapabilitySchemaNode), capability.id
        assert parameter.schema.type in CAPABILITY_STRUCTURAL_TYPES, capability.id
    for binding in capability.core_bindings:
        assert binding.reference and binding.anchors, capability.id
    assert capability.variants or any(
        binding.kind != "composition_evidence" and ":" in binding.reference
        for binding in capability.core_bindings
    ), capability.id
    if any(surface.surface == "API" and surface.present for surface in capability.surfaces):
        assert capability.variants, capability.id
    for variant in capability.variants:
        assert ":" in variant.reference and variant.source, capability.id
        assert not any(parameter.name == "argv" for parameter in variant.parameters), capability.id
        for parameter in variant.parameters:
            assert parameter.help.strip(), (capability.id, parameter.name)
            assert parameter.schema.type in CAPABILITY_STRUCTURAL_TYPES, capability.id
            assert parameter.kind in {
                "POSITIONAL_ONLY", "POSITIONAL_OR_KEYWORD", "KEYWORD_ONLY", "VAR_POSITIONAL", "VAR_KEYWORD",
            }, capability.id

try:
    CAPABILITIES[0].description = "mutation"
except FrozenInstanceError:
    pass
else:
    raise AssertionError("The declarative registry must be immutable")

print("capability registry: complete audit baseline preserved; every contract declared")
