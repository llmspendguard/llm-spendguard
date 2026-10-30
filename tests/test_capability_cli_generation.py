"""Generated CLI contracts preserve parser behavior and expose every command."""

import argparse
from contextlib import redirect_stderr, redirect_stdout
import io
import json
from dataclasses import replace
from unittest.mock import patch

from spendguard import cli
from spendguard.capability_cli_help_generation import build_capability_cli_help_root
from spendguard.capability_cli_parsing import add_registered_cli_argument
from spendguard.capability_registry import CLI_ARGUMENT_PROJECTIONS, CLI_COMMAND_GRAMMARS
from spendguard.capability_cli_callable_generation import registered_callable_cli_commands

CLI_PROJECTION_TEST_PORT = 42


def verify_registered_cli_help_projection():
    root = build_capability_cli_help_root()
    commands = next(action.choices for action in root._actions
                    if isinstance(action, argparse._SubParsersAction))
    expected = {name for grammar in CLI_COMMAND_GRAMMARS.values()
                for name in (grammar['command'], *grammar['aliases'])}
    expected.update(registered_callable_cli_commands())
    assert set(commands) == expected == set(cli._all_commands())
    for grammar in CLI_COMMAND_GRAMMARS.values():
        for name in (grammar['command'], *grammar['aliases']):
            output = io.StringIO()
            with redirect_stdout(output):
                assert cli._dispatch([name, '--help']) == 0
            help_text = output.getvalue()
            assert 'usage:' in help_text, name
            if grammar['takes_no_flags']:
                assert 'Takes no flags.' in help_text, name
            else:
                assert any(token.startswith('--') and token != '--help'
                           for token in help_text.split()), name
    for command, (_, variant) in registered_callable_cli_commands().items():
        help_text = commands[command].format_help()
        exposed = []
        for parameter in variant.parameters:
            if any(surface == 'CLI' for surface, _reason in parameter.surface_restrictions):
                assert '--' + parameter.name.replace('_', '-') not in help_text, command
                continue
            exposed.append(parameter)
            assert '--' + parameter.name.replace('_', '-') in help_text, command
        if not exposed:
            assert 'Takes no flags.' in help_text, command


def verify_registered_cli_dynamic_attribute_boundary():
    projection = next(item for item in CLI_ARGUMENT_PROJECTIONS.values()
                      if item.names == ('--port',) and item.commands == ('serve',))
    parser = argparse.ArgumentParser()
    # The caller supplies its already-evaluated default and converter. The
    # registry must neither evaluate the source expression nor change identity.
    default = object()
    action = add_registered_cli_argument(parser, projection.declaration_id,
                                         default=default, type=int)
    assert action.default is default
    assert parser.parse_args([]).port is default
    assert parser.parse_args(['--port', str(CLI_PROJECTION_TEST_PORT)]).port == CLI_PROJECTION_TEST_PORT
    with redirect_stdout(io.StringIO()):
        try:
            add_registered_cli_argument(argparse.ArgumentParser(), projection.declaration_id,
                                        default=default)
        except ValueError as error:
            assert 'dynamic attributes' in str(error)
        else:
            raise AssertionError('Missing runtime converter must fail closed')


def verify_generated_cli_core_binding_and_omission():
    from spendguard import capability_registry, capability_surface_generation
    from spendguard.capability_cli_callable_generation import invoke_callable_cli_command

    capability = next(item for item in capability_registry.CAPABILITIES
                      if any(variant.reference == 'spendguard.pricing:price' for variant in item.variants))
    variant = next(item for item in capability.variants if item.reference == 'spendguard.pricing:price')
    model_parameter = next(parameter for parameter in variant.parameters if parameter.name == 'model')
    keyword_parameter = replace(model_parameter, name='aliases', required=False, kind='VAR_KEYWORD',
                                schema=capability_registry.CapabilitySchemaNode(type='object'))
    variant = replace(variant, parameters=(model_parameter, keyword_parameter))
    capability = replace(capability, variants=(variant,))
    command = capability_surface_generation.capability_variant_export_name(capability, variant).replace('_', '-')
    capability = replace(capability, surfaces=tuple(
        replace(surface, present=True, names=(command,), absence_classification=None, reason='')
        if surface.surface == 'CLI' else surface for surface in capability.surfaces))
    observed = []

    def record_generated_cli_fixture_call(**arguments):
        observed.append(arguments)
        return arguments

    with patch.object(capability_registry, 'CAPABILITIES', (capability,)), patch.object(
        capability_surface_generation, 'resolve_capability_callable', return_value=record_generated_cli_fixture_call
    ), redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
        for invalid_arguments in ([], ['--model', __name__, '--aliases', json.dumps([])]):
            try:
                invoke_callable_cli_command(command, invalid_arguments)
            except SystemExit as error:
                assert error.code == 2
            else:
                raise AssertionError('Required arguments and keyword object types must fail closed')
        assert observed == []
        assert invoke_callable_cli_command(command, ['--model', __name__]) == (True, 0)
        assert invoke_callable_cli_command(command, ['--model', __name__, '--aliases', json.dumps({__name__: True})]) == (True, 0)
    assert observed == [{'model': __name__}, {'model': __name__, __name__: True}], observed


verify_registered_cli_help_projection()
verify_registered_cli_dynamic_attribute_boundary()
verify_generated_cli_core_binding_and_omission()
print('capability CLI generation: complete command help and parser compatibility verified')
