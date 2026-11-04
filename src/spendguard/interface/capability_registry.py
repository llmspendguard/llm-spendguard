"""Declarative, lazy capability inventory shared by user surfaces.

The baseline preserves audited exposure. ``contract_status`` deliberately distinguishes
reviewed parameter contracts from pending work; pending is never an empty-argument promise.
Importing this module loads data only and never imports a capability's implementation.
"""
from dataclasses import dataclass, field
from importlib.resources import files
from importlib import import_module
import json
from typing import Any, Literal

from .capability_cli_projection_models import (
    load_capability_cli_argument_projections,
    load_capability_cli_dispatch_bindings,
    load_capability_cli_grammars,
    load_capability_cli_manual_projections,
)


CAPABILITY_REGISTRY_RESOURCE = "capability_registry_data.jsonl"
CAPABILITY_MCP_CONTRACT_RESOURCE = "capability_mcp_contracts.json"

CLI_ARGUMENT_PROJECTIONS = load_capability_cli_argument_projections()
CLI_MANUAL_OPTION_PROJECTIONS = load_capability_cli_manual_projections()
CLI_COMMAND_GRAMMARS = load_capability_cli_grammars()
CLI_COMMAND_BINDINGS = load_capability_cli_dispatch_bindings()

# Reviewed against adapters.call, output_budget, and vendor_call.call. These are
# contract declarations consumed by generated documentation, not routing policy.
CAPABILITY_ROUTING_GUIDANCE = (
    "For measured money-saving routing, call adapters.call(..., reasoning='best-value', intent=...). "
    "An explicit best-value request may select both model and reasoning effort using measured quality for the intent; "
    "without qualifying evidence it keeps the requested model.",
    "When best-value is inherited from advisor.default_reasoning rather than explicitly requested, a supplied model "
    "stays pinned and the advisor selects its reasoning effort. no_substitution also pins the model.",
    "intent labels adapters.call work for attribution, quality evidence and routing. vendor_call.call uses purpose "
    "for the corresponding intent context and accepts reasoning, rather than an intent keyword.",
    "no_metered_fallback=True is the explicit subscription-only contract: a lane failure returns an error instead "
    "of billing a metered fallback. metered_only on adapters.call explicitly selects paid API execution.",
    "Normal adapters.call and vendor_call.call output budgets come from adapters.output_budget and the model's "
    "published output ceiling. Their max_tokens compatibility argument does not impose a caller-selected normal "
    "output cap; billing remains based on actual generated tokens. Input context limits are checked separately.",
    "vendor_call.call requires a positive deadline_s covering the whole call and every retry; attempts and backoff_s "
    "govern retry handling. adapters.call timeout_s bounds execution, and retries governs retry handling. Each paid "
    "attempt can bill; a shorter output argument is not a money budget.",
    "adapters.call measurement pins model attribution. temperature, top_p and seed are measurement controls that "
    "force paid execution and a pinned model; production calls reject these controls. Anthropic does not support seed.",
)


@dataclass(frozen=True)
class CapabilityMcpContract:
    """Canonical existing tool contract; argument omissions remain omissions."""

    name: str
    description: str
    schema: dict[str, Any]
    reference: str
    capability_ids: tuple[str, ...]


MCP_CONTRACTS = tuple(
    CapabilityMcpContract(**{**declaration, "capability_ids": tuple(declaration["capability_ids"])})
    for declaration in json.loads(
        files(__package__).joinpath(CAPABILITY_MCP_CONTRACT_RESOURCE).read_text(encoding="utf-8")
    )
)


@dataclass(frozen=True)
class CapabilitySchemaNode:
    type: str
    properties: tuple[tuple[str, "CapabilitySchemaNode"], ...] = ()
    items: "CapabilitySchemaNode | None" = None
    enum: tuple[Any, ...] = ()
    python_annotation: str | None = None
    alternatives: tuple["CapabilitySchemaNode", ...] = ()


@dataclass(frozen=True)
class CapabilityParameter:
    name: str
    schema: CapabilitySchemaNode
    required: bool
    default: Any
    default_declared: bool
    default_source: str | None
    help: str
    kind: str = "POSITIONAL_OR_KEYWORD"
    cli_projection_id: str | None = None
    surface_restrictions: tuple[tuple[str, str], ...] = ()
    json_conversion: str | None = None


@dataclass(frozen=True)
class CapabilityCallableVariant:
    reference: str
    parameters: tuple[CapabilityParameter, ...]
    returns: str
    documentation: str
    source: str
    constants: dict[str, Any] = field(default_factory=dict)
    parameter_renames: dict[str, str] = field(default_factory=dict)
    argument_mapping: bool = False
    surface_restrictions: tuple[tuple[str, str], ...] = ()
    result_serializer: str | None = None
    cli_names: tuple[str, ...] = ()
    cli_result_is_exit_code: bool = False


@dataclass(frozen=True)
class CapabilityCoreBinding:
    reference: str
    kind: Literal["public_core", "existing_handler", "composition_evidence"]
    anchors: tuple[str, ...]
    extraction_required: bool = False


@dataclass(frozen=True)
class CapabilitySurfaceExposure:
    surface: Literal["CLI", "API", "MCP"]
    present: bool
    names: tuple[str, ...]
    absence_classification: Literal["GAP", "DELIBERATE"] | None
    reason: str
    documentation: str | None
    evidence: tuple[str, ...]


@dataclass(frozen=True)
class Capability:
    id: str
    semantic_id: str
    description: str
    parameters: tuple[CapabilityParameter, ...]
    contract_status: Literal["reviewed_extraction", "source_declared", "pending"]
    contract_notes: str
    core_bindings: tuple[CapabilityCoreBinding, ...]
    surfaces: tuple[CapabilitySurfaceExposure, ...]
    variants: tuple[CapabilityCallableVariant, ...] = ()


def capability_schema_from_declaration(declaration: dict[str, Any]) -> CapabilitySchemaNode:
    """Decode a recursive declarative schema without importing runtime callables."""
    return CapabilitySchemaNode(
        type=declaration["type"],
        properties=tuple((key, capability_schema_from_declaration(value))
                         for key, value in declaration.get("properties", {}).items()),
        items=capability_schema_from_declaration(declaration["items"]) if declaration.get("items") else None,
        enum=tuple(declaration.get("enum", ())),
        python_annotation=declaration.get("python_annotation"),
        alternatives=tuple(capability_schema_from_declaration(value)
                           for value in declaration.get("alternatives", ())),
    )


def capability_parameter_from_declaration(parameter: dict[str, Any]) -> CapabilityParameter:
    """Preserve declaration defaults as data; omission never evaluates or injects them."""
    return CapabilityParameter(
        name=parameter["name"], schema=capability_schema_from_declaration(parameter["schema"]),
        required=parameter["required"], default=parameter.get("default"),
        default_declared=parameter.get("default_declared", False),
        default_source=parameter.get("default_source"), help=parameter["help"],
        kind=parameter.get("kind", "POSITIONAL_OR_KEYWORD"),
        cli_projection_id=parameter.get("cli_projection_id"),
        surface_restrictions=tuple(parameter.get("surface_restrictions", {}).items()),
        json_conversion=parameter.get("json_conversion"),
    )


def resolve_capability_callable(reference: str):
    """Resolve an explicitly declared module:qualname only when a surface invokes it."""
    module_name, qualified_name = reference.split(":", 1)
    bound_callable = import_module(module_name)
    for component in qualified_name.split("."):
        bound_callable = getattr(bound_callable, component)
    if not callable(bound_callable):
        raise TypeError(f"Capability reference is not callable: {reference}")
    return bound_callable


def load_capability_registry() -> tuple[Capability, ...]:
    """Read the packaged baseline; pending contracts remain explicit and fail closed."""
    resource = files(__package__).joinpath(CAPABILITY_REGISTRY_RESOURCE)
    capabilities = []
    for line in resource.read_text(encoding="utf-8").splitlines():
        declaration = json.loads(line)
        capabilities.append(Capability(
            id=declaration["id"], semantic_id=declaration["semantic_id"],
            description=declaration["description"],
            parameters=tuple(capability_parameter_from_declaration(parameter)
                             for parameter in declaration["parameters"]),
            contract_status=declaration["contract_status"], contract_notes=declaration["contract_notes"],
            core_bindings=tuple(CapabilityCoreBinding(
                reference=binding["reference"], kind=binding["kind"],
                anchors=tuple(binding["anchors"]), extraction_required=binding.get("extraction_required", False),
            ) for binding in declaration["core_bindings"]),
            surfaces=tuple(CapabilitySurfaceExposure(
                surface=surface["surface"], present=surface["present"], names=tuple(surface["names"]),
                absence_classification=surface["absence_classification"], reason=surface["reason"],
                documentation=surface["documentation"], evidence=tuple(surface["evidence"]),
            ) for surface in declaration["surfaces"]),
            variants=tuple(CapabilityCallableVariant(
                reference=variant["reference"],
                parameters=tuple(capability_parameter_from_declaration(parameter)
                                 for parameter in variant["parameters"]),
                returns=variant["returns"], documentation=variant["documentation"], source=variant["source"],
                constants=variant.get("constants", {}),
                parameter_renames=variant.get("parameter_renames", {}),
                argument_mapping=variant.get("argument_mapping", False),
                surface_restrictions=tuple(variant.get("surface_restrictions", {}).items()),
                result_serializer=variant.get("result_serializer"),
                cli_names=tuple(variant.get("cli_names", ())),
                cli_result_is_exit_code=variant.get("cli_result_is_exit_code", False),
            ) for variant in declaration.get("variants", [])),
        ))
    return tuple(capabilities)


CAPABILITIES = load_capability_registry()
