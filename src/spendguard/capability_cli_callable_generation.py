"""Generate additive callable CLI commands from canonical capability schemas."""

import argparse
import json


def registered_callable_cli_commands():
    """Project explicitly exposed variant command names from registry data."""
    from .capability_registry import CAPABILITIES
    from .capability_surface_generation import capability_variant_export_name

    commands = {}
    for capability in CAPABILITIES:
        exposure = next(surface for surface in capability.surfaces if surface.surface == 'CLI')
        if not exposure.present:
            continue
        for variant in capability.variants:
            names = variant.cli_names or (capability_variant_export_name(capability, variant).replace('_', '-'),)
            for command in names:
                if command in exposure.names:
                    if command in commands:
                        raise ValueError(f'Duplicate generated CLI command: {command}')
                    commands[command] = (capability, variant)
    return commands


def decode_callable_cli_json(value):
    """Accept a complete JSON value; malformed input is an argparse error."""
    try:
        return json.loads(value)
    except json.JSONDecodeError as error:
        raise argparse.ArgumentTypeError(str(error)) from error


def populate_callable_cli_parser(parser, capability, variant):
    """Declare real flags from canonical types, preserving omitted arguments."""
    parser.description = capability.description
    exposed_parameters = []
    for parameter in variant.parameters:
        if any(surface == 'CLI' for surface, _reason in parameter.surface_restrictions):
            continue
        exposed_parameters.append(parameter)
        converter = {'string': str, 'integer': int, 'number': float}.get(
            parameter.schema.type, decode_callable_cli_json)
        help_text = parameter.help
        if converter is decode_callable_cli_json:
            help_text += ' Supply a JSON value, including quotes around JSON strings.'
        option = '--' + parameter.name.replace('_', '-')
        if parameter.schema.type == 'boolean' and parameter.default is False:
            parser.add_argument(option, dest=parameter.name, action='store_true', default=argparse.SUPPRESS,
                                required=parameter.required, help=help_text)
        else:
            parser.add_argument(option, dest=parameter.name, type=converter,
                                required=parameter.required and parameter.kind not in {'VAR_POSITIONAL', 'VAR_KEYWORD'},
                                default=argparse.SUPPRESS, help=help_text)
    if not exposed_parameters:
        parser.epilog = 'Takes no flags.'


def invoke_callable_cli_command(command, rest):
    """Call the same bound core and serialize its report without business logic."""
    from .capability_surface_generation import capture_capability_tool_call

    declaration = registered_callable_cli_commands().get(command)
    if declaration is None:
        return False, None
    capability, variant = declaration
    parser = argparse.ArgumentParser(prog='spendguard ' + command)
    populate_callable_cli_parser(parser, capability, variant)
    arguments = vars(parser.parse_args(rest))
    for parameter in variant.parameters:
        if parameter.kind == 'VAR_POSITIONAL' and parameter.name in arguments:
            if not isinstance(arguments[parameter.name], list):
                parser.error(f'--{parameter.name.replace("_", "-")} requires a JSON array')
        if parameter.kind == 'VAR_KEYWORD' and parameter.name in arguments:
            additional = arguments.pop(parameter.name)
            if not isinstance(additional, dict):
                parser.error(f'--{parameter.name.replace("_", "-")} requires a JSON object')
            duplicate = set(additional).intersection(arguments)
            if duplicate:
                parser.error(f'Duplicate arguments in keyword object: {sorted(duplicate)}')
            arguments.update(additional)
    if variant.cli_result_is_exit_code:
        from .capability_surface_generation import invoke_capability_tool_variant

        result = invoke_capability_tool_variant(variant, arguments)
        if not isinstance(result, int) or isinstance(result, bool):
            raise TypeError('Registered CLI exit-code result must be an integer')
        return True, result
    print(json.dumps(capture_capability_tool_call(variant, arguments), ensure_ascii=False))
    return True, 0
