"""Generate lazy API contracts and MCP callable tools from capability declarations."""
from .capability_registry import CAPABILITIES, resolve_capability_callable


def normalize_capability_tool_result(value):
    """Preserve structured return values using standard JSON representations."""
    from dataclasses import asdict, is_dataclass
    from datetime import date, datetime
    from enum import Enum
    from decimal import Decimal
    from pathlib import PurePath
    from collections.abc import Mapping

    if is_dataclass(value) and not isinstance(value, type):
        return normalize_capability_tool_result(asdict(value))
    if isinstance(value, Mapping):
        if all(isinstance(key, str) for key in value):
            return {key: normalize_capability_tool_result(item) for key, item in value.items()}
        return {"entries": [
            {"key": normalize_capability_tool_result(key), "value": normalize_capability_tool_result(item)}
            for key, item in value.items()
        ]}
    if isinstance(value, (tuple, list)):
        return [normalize_capability_tool_result(item) for item in value]
    if isinstance(value, (set, frozenset)):
        import json
        normalized = [normalize_capability_tool_result(item) for item in value]
        return sorted(normalized, key=lambda item: json.dumps(item, sort_keys=True))
    if isinstance(value, Enum):
        return normalize_capability_tool_result(value.value)
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, PurePath):
        return str(value)
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    raise TypeError(f"Capability returned unsupported JSON value: {type(value).__qualname__}")


def capture_capability_tool_call(variant, arguments):
    """Keep printed reports out of MCP stdio and retain them as explicit output."""
    from contextlib import redirect_stdout, redirect_stderr
    from io import StringIO

    output, errors = StringIO(), StringIO()
    with redirect_stdout(output), redirect_stderr(errors):
        result = invoke_capability_tool_variant(variant, arguments)
    if variant.result_serializer == "ask_result_as_dict":
        result = result.as_dict()
    elif variant.result_serializer == "overage_attribution_with_evidence":
        result = {"attribution": dict(result), "discarded_evidence": result.discarded_evidence}
    payload = normalize_capability_tool_result(result)
    if output.getvalue() or errors.getvalue():
        return {"result": payload, "stdout": output.getvalue(), "stderr": errors.getvalue()}
    return payload


def capability_contract_documentation(capability, variant):
    """Render documentation from declared descriptions and parameter contracts."""
    lines = [capability.description, "", variant.documentation, "", "Parameters:"]
    lines.extend(f"{surface} DELIBERATE restriction: {reason}"
                 for surface, reason in variant.surface_restrictions)
    if variant.result_serializer:
        lines.append(f"JSON return serializer: {variant.result_serializer}.")
    lines.extend(
        f"  {parameter.name} ({parameter.schema.python_annotation or parameter.schema.type}): "
        f"{parameter.help} Required: {parameter.required}."
        + (f" Default: {parameter.default_source}." if parameter.default_declared else "")
        + "".join(f" {surface} DELIBERATE restriction: {reason}."
                  for surface, reason in parameter.surface_restrictions)
        + (f" JSON transport conversion: {parameter.json_conversion}." if parameter.json_conversion else "")
        for parameter in variant.parameters
    )
    return "\n".join(lines)


def capability_variant_export_name(capability, variant):
    """Use the declared implementation identity to distinguish callable variants."""
    module_name, qualified_name = variant.reference.split(":", 1)
    return "capability_" + capability.semantic_id.replace("-", "_") + "_" + (
        module_name.removeprefix("spendguard.") + "_" + qualified_name
    ).replace(".", "_")


class CapabilityApiCallable:
    """A lazy callable whose discovered signature is the actual bound core contract."""

    def __init__(self, capability, variant):
        self.capability = capability
        self.variant = variant
        self.__name__ = capability_variant_export_name(capability, variant)
        self.__doc__ = capability_contract_documentation(capability, variant)

    @property
    def __signature__(self):
        from inspect import signature
        core_signature = signature(resolve_capability_callable(self.variant.reference))
        from inspect import Parameter, Signature
        if self.variant.argument_mapping:
            return Signature([
                Parameter(parameter.name, Parameter.KEYWORD_ONLY,
                          default=Parameter.empty if parameter.required else parameter.default)
                for parameter in self.variant.parameters
            ])
        inverse = {target: source for source, target in self.variant.parameter_renames.items()}
        return core_signature.replace(parameters=[
            parameter.replace(name=inverse.get(parameter.name, parameter.name))
            for parameter in core_signature.parameters.values()
            if parameter.name not in self.variant.constants
        ])

    def __call__(self, *args, **kwargs):
        if self.variant.constants or self.variant.parameter_renames or self.variant.argument_mapping:
            supplied = self.__signature__.bind(*args, **kwargs).arguments
            return invoke_capability_tool_variant(self.variant, supplied)
        return resolve_capability_callable(self.variant.reference)(*args, **kwargs)


def make_capability_api_wrapper(capability, variant):
    """Expose the exact core's signature lazily and preserve all argument omissions."""
    return CapabilityApiCallable(capability, variant)

def build_capability_api_exports():
    """Every declared callable variant has a discoverable lazy root export."""
    exports = {}
    coverage = {}
    for capability in CAPABILITIES:
        api_exposure = next(surface for surface in capability.surfaces if surface.surface == "API")
        if api_exposure.absence_classification == "DELIBERATE":
            continue
        coverage[capability.id] = []
        for variant in capability.variants:
            wrapper = make_capability_api_wrapper(capability, variant)
            if wrapper.__name__ in exports:
                raise ValueError(f"Duplicate generated capability export: {wrapper.__name__}")
            exports[wrapper.__name__] = wrapper
            coverage[capability.id].append(wrapper.__name__)
    return exports, coverage


def document_existing_capability_exports(namespace):
    """Attach generated contracts to existing functions without replacing their identities."""
    from inspect import isfunction

    contracts = {variant.reference: (capability, variant)
                 for capability in CAPABILITIES for variant in capability.variants}
    for value in list(namespace.values()):
        if not isfunction(value):
            continue
        reference = value.__module__ + ":" + value.__qualname__
        declaration = contracts.get(reference)
        if declaration is not None:
            value.__doc__ = capability_contract_documentation(*declaration)


def capability_json_schema(schema):
    """Translate declared JSON-compatible type structure without runtime guessing."""
    result = {}
    if schema.alternatives:
        result["anyOf"] = [capability_json_schema(part) for part in schema.alternatives]
    elif schema.type in {"string", "number", "integer", "boolean", "array", "object", "null"}:
        result["type"] = schema.type
    if schema.properties:
        result["properties"] = {key: capability_json_schema(value) for key, value in schema.properties}
    if schema.items is not None:
        result["items"] = capability_json_schema(schema.items)
    if schema.enum:
        result["enum"] = list(schema.enum)
    return result


def capability_variant_tool_schema(variant):
    """Publish canonical parameter help and requiredness without injecting defaults."""
    properties = {}
    required = []
    additional = False
    for parameter in variant.parameters:
        if any(surface == "MCP" for surface, _ in parameter.surface_restrictions):
            continue
        if parameter.kind == "VAR_KEYWORD":
            additional = True
            continue
        properties[parameter.name] = capability_json_schema(parameter.schema)
        properties[parameter.name]["description"] = parameter.help
        if parameter.required and parameter.kind != "VAR_POSITIONAL":
            required.append(parameter.name)
    result = {"type": "object", "properties": properties, "additionalProperties": additional}
    if required:
        result["required"] = required
    return result


def invoke_capability_tool_variant(variant, arguments):
    """Adapt JSON argument names to the actual core's Python calling convention."""
    arguments = dict(arguments)
    for parameter in variant.parameters:
        if parameter.name not in arguments:
            continue
        if parameter.surface_restrictions:
            raise TypeError(f"Python-only control {parameter.name}: {dict(parameter.surface_restrictions)}")
        if arguments[parameter.name] is not None and parameter.json_conversion:
            from datetime import date, datetime
            converters = {"iso_date": date.fromisoformat, "iso_datetime": datetime.fromisoformat}
            arguments[parameter.name] = converters[parameter.json_conversion](arguments[parameter.name])
    supplied = {
        variant.parameter_renames.get(name, name): value for name, value in arguments.items()
    }
    supplied.update(variant.constants)
    if variant.argument_mapping:
        return resolve_capability_callable(variant.reference)(supplied)
    positional = []
    for parameter in variant.parameters:
        if parameter.kind == "POSITIONAL_ONLY":
            if variant.parameter_renames.get(parameter.name, parameter.name) in supplied:
                positional.append(supplied.pop(variant.parameter_renames.get(parameter.name, parameter.name)))
            elif parameter.required:
                raise TypeError(f"Missing positional-only argument: {parameter.name}")
            else:
                # Later positional-only arguments cannot bypass an omitted predecessor.
                if any(later.name in supplied for later in variant.parameters[
                    variant.parameters.index(parameter) + 1:
                ] if later.kind == "POSITIONAL_ONLY"):
                    raise TypeError(f"Supply preceding positional-only argument: {parameter.name}")
        elif parameter.kind == "VAR_POSITIONAL":
            positional.extend(supplied.pop(parameter.name, ()))
    return resolve_capability_callable(variant.reference)(*positional, **supplied)


def build_generated_capability_tools():
    """Build additive tools and an exact capability-to-tool coverage inventory."""
    tools = {}
    coverage = {}
    for capability in CAPABILITIES:
        exposure = next(surface for surface in capability.surfaces if surface.surface == "MCP")
        if exposure.absence_classification == "DELIBERATE":
            continue
        coverage[capability.id] = []
        for variant in capability.variants:
            if any(surface == "MCP" for surface, _ in variant.surface_restrictions):
                continue
            name = capability_variant_export_name(capability, variant)
            def invoke_declared_tool(arguments, bound_variant=variant):
                return capture_capability_tool_call(bound_variant, arguments)
            tools[name] = (
                capability_contract_documentation(capability, variant),
                capability_variant_tool_schema(variant),
                invoke_declared_tool,
            )
            coverage[capability.id].append(name)
    return tools, coverage
