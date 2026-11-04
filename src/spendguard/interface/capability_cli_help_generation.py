"""Construct discoverable CLI help from the registry's parser projections."""
from __future__ import annotations

import argparse
import ast
from typing import Any

CAPABILITY_CLI_GROUPED_INTRO = (
    'spendguard — know what an LLM job will cost before you run it, and prove your ledger matches the bill.',
    '',
    'quickstart:  pip install llm-spendguard[all]  &&  spendguard init --all',
    '             (auto-detects your $0 subscription lanes + wires the gate, MCP tools, receipt & slash-commands)',
    '', 'usage: spendguard <command> [args]    ·    spendguard --version', '',
)
CAPABILITY_CLI_GROUPED_FOOTER = (
    'Not listed here: the deeper surface (bootstrap, insights, compare, experiment internals, tag, …).',
    'Full reference: https://docs.llmspendguard.com/CLI/  ·  every setting: `spendguard config`',
)
CAPABILITY_CLI_GROUPED_COMMAND_WIDTH = 15


def render_capability_cli_grouped_help() -> str:
    """Keep the legacy overview while deriving its command entries from the registry."""
    from .capability_registry import CLI_COMMAND_GRAMMARS

    entries = sorted(
        (entry for contract in CLI_COMMAND_GRAMMARS.values()
         for entry in contract.get('help_entries', ())),
        key=lambda entry: (entry['group_order'], entry['entry_order']),
    )
    lines = list(CAPABILITY_CLI_GROUPED_INTRO)
    previous_group = None
    for entry in entries:
        if entry['group'] != previous_group:
            if previous_group is not None:
                lines.append('')
            lines.append(entry['group'] + ':')
            previous_group = entry['group']
        lines.append(f"  {entry['name']:<{CAPABILITY_CLI_GROUPED_COMMAND_WIDTH}} {entry['description']}")
    lines.extend(('', *CAPABILITY_CLI_GROUPED_FOOTER))
    return '\n'.join(lines)


def cli_projection_documented_value(record: dict[str, Any]) -> Any:
    """Decode display metadata without evaluating source or importing handlers."""
    if 'literal' in record:
        return record['literal']
    expression = record['expression']
    structural = {'int': int, 'float': float, 'str': str,
                  'argparse.SUPPRESS': argparse.SUPPRESS,
                  'argparse.BooleanOptionalAction': argparse.BooleanOptionalAction}
    if expression in structural:
        return structural[expression]
    syntax = ast.parse(expression, mode='eval').body
    if isinstance(syntax, ast.JoinedStr):
        return ''.join(part.value if isinstance(part, ast.Constant) else '{'+ast.unparse(part.value)+'}' for part in syntax.values)
    if isinstance(syntax, ast.BinOp) and isinstance(syntax.op, ast.Mod) and isinstance(syntax.left, ast.Constant):
        return syntax.left.value.replace('%d', '{'+ast.unparse(syntax.right)+'}')
    return None


def populate_capability_cli_help_parser(parser: argparse.ArgumentParser, contract: dict[str, Any]) -> None:
    """Replay declarative parser topology, then add manual compatibility controls."""
    from .capability_registry import CLI_ARGUMENT_PROJECTIONS, CLI_MANUAL_OPTION_PROJECTIONS

    registered_names: set[str] = set()
    graphs = contract['parser_graphs']
    # Composite commands use named provider/mode child grammars; normal commands
    # have one constructor graph. Keep independent grammars visibly separate.
    graph_parent = parser
    graph_children = parser.add_subparsers(dest='mode') if len(graphs)>1 else None
    for graph in graphs:
        if graph_children is not None:
            graph_parent = graph_children.add_parser(graph['selector'])
        owners: dict[str, Any] = {}
        for operation in graph['operations']:
            kind = operation['operation']
            if kind == 'ArgumentParser':
                owners[operation['target']] = graph_parent
                continue
            if kind == 'add_argument':
                projection = CLI_ARGUMENT_PROJECTIONS[operation['declaration_id']]
                owner = owners[operation['owner']]
                attributes = dict(projection.literal_attributes)
                for name, expression in projection.dynamic_attributes.items():
                    value = cli_projection_documented_value({'expression':expression})
                    if value is not None:
                        attributes[name] = value
                owner.add_argument(*projection.names, **attributes)
                if owner is parser:
                    registered_names.update(projection.names)
                continue
            owner = owners[operation['owner']]
            args = [cli_projection_documented_value(r) for r in operation['args']]
            attributes = {name: cli_projection_documented_value(record) for name,record in operation['attributes'].items()}
            owners[operation['target']] = getattr(owner, kind)(*args, **attributes)
    for token_id in contract['manual_token_ids']:
        parameter = CLI_MANUAL_OPTION_PROJECTIONS[token_id]
        spelling = parameter['spelling']
        if spelling in registered_names or parameter['json_type']=='separator':
            continue
        attributes = {'help': parameter['help']}
        if parameter['cli_action']=='store_true':
            attributes['action']='store_true'
        else:
            if parameter['cli_nargs']=='*':
                attributes['nargs']='*'
            attributes['type']={'integer':int,'number':float}.get(parameter['json_type'],str)
        parser.add_argument(spelling, **attributes)
        registered_names.add(spelling)
    if contract['takes_no_flags']:
        parser.epilog='Takes no flags. Positional arguments, when supported, retain the documented legacy grammar.'
    else:
        nested_help = []
        for action in parser._actions:
            if isinstance(action, argparse._SubParsersAction):
                for name, child in action.choices.items():
                    nested_help.append(f'{name}:\n{child.format_help()}')
        if nested_help:
            parser.epilog='\n'.join(nested_help)
            parser.formatter_class=argparse.RawDescriptionHelpFormatter


def build_capability_cli_help_root() -> argparse.ArgumentParser:
    """Build all canonical command parsers and aliases from registry contracts."""
    from .capability_registry import CAPABILITIES, CLI_COMMAND_GRAMMARS

    parser=argparse.ArgumentParser(prog='spendguard')
    subparsers=parser.add_subparsers(dest='command')
    descriptions = {}
    for capability in CAPABILITIES:
        for exposure in capability.surfaces:
            if exposure.surface == 'CLI' and exposure.present:
                for invocation in exposure.names:
                    descriptions.setdefault(invocation, capability.description)
    for contract in CLI_COMMAND_GRAMMARS.values():
        description=descriptions.get(contract['command'],contract['nested_grammar_summary'] or contract['command'])
        command_parser=subparsers.add_parser(contract['command'],aliases=contract['aliases'],description=description,help=description)
        populate_capability_cli_help_parser(command_parser, contract)
    from .capability_cli_callable_generation import populate_callable_cli_parser, registered_callable_cli_commands

    for command, (capability, variant) in registered_callable_cli_commands().items():
        command_parser = subparsers.add_parser(command, help=capability.description)
        populate_callable_cli_parser(command_parser, capability, variant)
    return parser


def render_capability_cli_help(command: str | None = None) -> str:
    """Render root or command help without invoking capability implementations."""
    root = build_capability_cli_help_root()
    if command is None:
        return root.format_help()
    for action in root._actions:
        if isinstance(action, argparse._SubParsersAction) and command in action.choices:
            return action.choices[command].format_help()
    return f'usage: spendguard {command} [args]\n\nspendguard command\n'
