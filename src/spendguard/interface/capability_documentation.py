"""Render the capability reference from the same declarations used by surfaces."""
from collections import Counter
from dataclasses import asdict
import hashlib
import json


def capability_reference_cell(value):
    """Escape table syntax without dropping any contract content."""
    return str(value).replace("|", "\\|").replace("\n", "<br>")


def capability_reference_anchor(reference):
    """Stable exact-reference anchor; no lossy semantic-name canonicalisation."""
    return "core-" + hashlib.sha256(reference.encode("utf-8")).hexdigest()


def render_capability_parameter_contract(parameters):
    """Keep omission, literal defaults and source defaults distinct."""
    if not parameters:
        return ["Takes no parameters.", ""]
    lines = ["| Parameter | Type | Required | Default | Contract |",
             "| --- | --- | --- | --- | --- |"]
    for parameter in parameters:
        if not parameter.default_declared:
            default = "required" if parameter.required else "omitted; implementation decides"
        elif parameter.default_source:
            default = parameter.default_source
        else:
            default = json.dumps(parameter.default, ensure_ascii=False)
        declared_type = parameter.schema.python_annotation or parameter.schema.type
        if parameter.schema.properties or parameter.schema.items or parameter.schema.alternatives or parameter.schema.enum:
            declared_type += " " + json.dumps(asdict(parameter.schema), ensure_ascii=False, sort_keys=True)
        contract = parameter.help
        if parameter.json_conversion:
            contract += f" JSON transport: {parameter.json_conversion}."
        for surface, reason in parameter.surface_restrictions:
            contract += f" {surface} DELIBERATE parameter restriction: {reason}"
        cells = (parameter.name, declared_type, "yes" if parameter.required else "no",
                 default, contract)
        lines.append("| " + " | ".join(capability_reference_cell(cell) for cell in cells) + " |")
    lines.append("")
    return lines


def render_capability_reference(capabilities, *, routing_guidance=()):
    """Generate capability discovery and unique core contracts without operational imports."""
    lines = ["# Capability reference", "", "Generated from the capability registry. Edit the registry, then regenerate this reference.", ""]
    if routing_guidance:
        lines.extend(["## Cost and routing controls", ""])
        for paragraph in routing_guidance:
            lines.extend([paragraph, ""])
    lines.extend(["## Capability index", "", "Each absence below carries the registry's reviewed reason.", "",
                  "| ID | Capability | CLI | API | MCP | Core contracts |",
                  "| --- | --- | --- | --- | --- | --- |"])
    counts = Counter()
    core_variants = {}
    for capability in capabilities:
        exposure_cells = {}
        for exposure in capability.surfaces:
            counts["PRESENT" if exposure.present else "ABSENT"] += 1
            if exposure.present:
                exposure_cells[exposure.surface] = "; ".join(exposure.names)
            else:
                exposure_cells[exposure.surface] = f"ABSENT ({exposure.absence_classification}): {exposure.reason}"
                counts[exposure.absence_classification] += 1
        contracts = []
        for variant in capability.variants:
            core_variants.setdefault(variant.reference, variant)
            contracts.append(f"[{variant.reference}](#{capability_reference_anchor(variant.reference)})")
        cells = (capability.id, capability.description, exposure_cells.get("CLI", "UNREVIEWED"),
                 exposure_cells.get("API", "UNREVIEWED"), exposure_cells.get("MCP", "UNREVIEWED"), "; ".join(contracts))
        lines.append("| " + " | ".join(capability_reference_cell(cell) for cell in cells) + " |")
    lines.extend(["", "Counts: " + "; ".join(f"{key} {value}" for key, value in sorted(counts.items())), "",
                  "## Capability parameter contracts", ""])
    for capability in capabilities:
        lines.extend([f"### {capability.id}: {capability.description}", "", capability.contract_notes, ""])
        lines.extend(render_capability_parameter_contract(capability.parameters))
    lines.extend([
                  "## Programmatic core contracts", ""])
    for reference, variant in sorted(core_variants.items()):
        lines.extend([f'<a id="{capability_reference_anchor(reference)}"></a>', "", f"### `{reference}`", "",
                      f"Source: `{variant.source}`", ""])
        if variant.documentation:
            lines.extend([variant.documentation, ""])
        for surface, reason in variant.surface_restrictions:
            lines.extend([f"{surface} DELIBERATE variant restriction: {reason}", ""])
        if variant.result_serializer:
            lines.extend([f"Declared result transport: `{variant.result_serializer}`.", ""])
        lines.extend(render_capability_parameter_contract(variant.parameters))
        lines.extend([f"Returns: `{variant.returns}`", ""])
    return "\n".join(lines)

