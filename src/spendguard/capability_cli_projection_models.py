"""Immutable CLI parser projections owned by the capability registry."""

from __future__ import annotations

from dataclasses import dataclass
from importlib.resources import files
import json
from types import MappingProxyType
from typing import Any, Mapping

CAPABILITY_CLI_ARGUMENT_RESOURCE = 'capability_cli_projection_data.json'
CAPABILITY_CLI_MANUAL_RESOURCE = 'capability_cli_manual_schema_data.json'
CAPABILITY_CLI_GRAMMAR_RESOURCE = 'capability_cli_grammar_data.json'


@dataclass(frozen=True)
class CapabilityCliArgumentProjection:
    """One declaration shared by command execution and generated documentation."""

    declaration_id: str
    commands: tuple[str, ...]
    names: tuple[str, ...]
    literal_attributes: Mapping[str, Any]
    dynamic_attributes: Mapping[str, str]
    evidence: str


def load_capability_cli_argument_projections() -> Mapping[str, CapabilityCliArgumentProjection]:
    """Load the registry's packaged declarative CLI argument projections."""
    resource = files('spendguard').joinpath(CAPABILITY_CLI_ARGUMENT_RESOURCE)
    records = json.loads(resource.read_text(encoding='utf-8'))
    projections = {}
    for record in records:
        attributes = record['attributes']
        projection = CapabilityCliArgumentProjection(
            declaration_id=record['declaration_id'],
            commands=tuple(record['commands']),
            names=tuple(record['names']),
            literal_attributes=MappingProxyType({
                key: tuple(value['literal']) if value.get('literal_container') == 'tuple' else value['literal']
                for key, value in attributes.items() if 'literal' in value
            }),
            dynamic_attributes=MappingProxyType({key: value['expression'] for key, value in attributes.items() if 'expression' in value}),
            evidence=record['evidence'],
        )
        if projection.declaration_id in projections:
            raise ValueError(f'Duplicate CLI declaration {projection.declaration_id}')
        projections[projection.declaration_id] = projection
    return MappingProxyType(projections)


def load_capability_cli_manual_projections() -> Mapping[str, Mapping[str, Any]]:
    """Load reviewed raw-argv controls without changing compatibility policy."""
    records = json.loads(files('spendguard').joinpath(CAPABILITY_CLI_MANUAL_RESOURCE).read_text(encoding='utf-8'))
    return MappingProxyType({record['token_id']: MappingProxyType(record) for record in records})


def load_capability_cli_grammars() -> Mapping[str, Mapping[str, Any]]:
    """Load canonical command/alias topology and explicit no-flags contracts."""
    records = json.loads(files('spendguard').joinpath(CAPABILITY_CLI_GRAMMAR_RESOURCE).read_text(encoding='utf-8'))
    return MappingProxyType({record['command']: MappingProxyType(record) for record in records})
