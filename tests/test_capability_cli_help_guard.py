"""Actual CLI command help must expose registry controls or an explicit no-flags contract."""
import argparse
from dataclasses import asdict
import json
from pathlib import Path
from contextlib import redirect_stdout
from io import StringIO

from spendguard import cli
from spendguard.capability_cli_help_generation import build_capability_cli_help_root
from spendguard.capability_registry import CLI_COMMAND_BINDINGS, CLI_COMMAND_GRAMMARS

CAPABILITY_LEGACY_CLI_CONTRACT_PATH = Path(__file__).parent / 'fixtures' / 'capability_legacy_cli_contract.json'


def assert_capability_legacy_cli_contract_is_preserved():
    """Freeze the pre-registry command spellings, transports and grouped discovery contract."""
    contract = json.loads(CAPABILITY_LEGACY_CLI_CONTRACT_PATH.read_text())
    for name, expected in contract['legacy_dispatch_bindings'].items():
        assert name in CLI_COMMAND_BINDINGS, f'Legacy dispatch missing: {name}'
        actual = asdict(CLI_COMMAND_BINDINGS[name])
        actual.pop('source_evidence')
        actual = {key: value for key, value in actual.items() if value is not None}
        assert actual == expected, (name, actual, expected)
    registered = set(cli._all_commands())
    assert not any(name.startswith('capability-') for name in registered), registered
    for control in contract['help_controls']:
        output = StringIO()
        with redirect_stdout(output):
            assert cli._dispatch([control]) == 0, control
        rendered = output.getvalue()
        assert 'Full reference:' in rendered, control
        for entry in contract['grouped_help_entries']:
            assert entry['group'] + ':' in rendered, (control, entry)
            assert entry['name'] in registered, entry
            assert entry['description'] in rendered, (control, entry)
    root = build_capability_cli_help_root()
    advertised = next(action.choices for action in root._actions if isinstance(action, argparse._SubParsersAction))
    assert set(advertised) == registered, (set(advertised) - registered, registered - set(advertised))


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
    assert_capability_legacy_cli_contract_is_preserved()
    assert_capability_cli_help_is_complete()
    print('CLI help guard: every actual command exposes flags or its no-flags declaration')
