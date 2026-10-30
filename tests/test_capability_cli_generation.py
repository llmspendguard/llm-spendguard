"""Generated CLI contracts preserve parser behavior and expose every command."""

import argparse
from contextlib import redirect_stdout
import io

from spendguard import cli
from spendguard.capability_cli_help_generation import build_capability_cli_help_root
from spendguard.capability_cli_parsing import add_registered_cli_argument
from spendguard.capability_registry import CLI_ARGUMENT_PROJECTIONS, CLI_COMMAND_GRAMMARS

CLI_PROJECTION_TEST_PORT = 42


def verify_registered_cli_help_projection():
    root = build_capability_cli_help_root()
    commands = next(action.choices for action in root._actions
                    if isinstance(action, argparse._SubParsersAction))
    expected = {name for grammar in CLI_COMMAND_GRAMMARS.values()
                for name in (grammar['command'], *grammar['aliases'])}
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


verify_registered_cli_help_projection()
verify_registered_cli_dynamic_attribute_boundary()
print('capability CLI generation: complete command help and parser compatibility verified')
