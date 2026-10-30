"""Actual CLI command help must expose registry controls or an explicit no-flags contract."""
import argparse
from contextlib import redirect_stdout
from io import StringIO

from spendguard import cli
from spendguard.capability_cli_help_generation import build_capability_cli_help_root
from spendguard.capability_registry import CLI_COMMAND_GRAMMARS


def assert_capability_cli_help_is_complete(dispatch=cli._dispatch):
    """Exercise the installed dispatch path for every actual canonical command."""
    root = build_capability_cli_help_root()
    choices = next(action.choices for action in root._actions if isinstance(action, argparse._SubParsersAction))
    failures = []
    for name in CLI_COMMAND_GRAMMARS:
        assert name in choices, f"CLI parser missing: {name}"
    for name in choices:
        contract = CLI_COMMAND_GRAMMARS.get(name, {})
        output = StringIO()
        with redirect_stdout(output):
            try:
                dispatch([name, '--help'])
            except SystemExit as error:
                assert error.code in (None, 0), (name, error.code)
        help_text = output.getvalue()
        if contract.get('takes_no_flags'):
            if 'Takes no flags.' not in help_text:
                failures.append(f'{name}: missing explicit no-flags declaration')
            continue
        parsers = [choices[name]]
        flags = set()
        while parsers:
            parser = parsers.pop()
            for action in parser._actions:
                if isinstance(action, argparse._SubParsersAction):
                    parsers.extend(action.choices.values())
                elif not isinstance(action, argparse._HelpAction):
                    flags.update(action.option_strings)
        if not flags and 'Takes no flags.' in help_text:
            continue
        if not flags or not any(flag in help_text for flag in flags):
            failures.append(f'{name}: help exposes no real declared flag')
    assert not failures, '\n'.join(failures)


if __name__ == '__main__':
    assert_capability_cli_help_is_complete()
    print('CLI help guard: every actual command exposes flags or its no-flags declaration')

