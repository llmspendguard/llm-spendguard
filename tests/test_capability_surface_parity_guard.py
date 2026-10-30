"""Capability exposure must exist in actual surface dispatch tables and exports."""
import argparse

import spendguard
from spendguard import mcp_server
from spendguard.capability_cli_help_generation import build_capability_cli_help_root
from spendguard.capability_registry import CAPABILITIES, CLI_COMMAND_BINDINGS
from spendguard.capability_cli_callable_generation import registered_callable_cli_commands


def assert_capability_actual_surface_parity():
    """Use coverage maps only to locate actual callable exports and dispatch entries."""
    root = build_capability_cli_help_root()
    cli_commands = next(action.choices for action in root._actions
                        if isinstance(action, argparse._SubParsersAction))
    failures = []
    generated_cli_commands = registered_callable_cli_commands()
    for capability in CAPABILITIES:
        for exposure in capability.surfaces:
            identity = f'{capability.id}/{exposure.surface}'
            if exposure.absence_classification == 'DELIBERATE':
                if not exposure.reason.strip():
                    failures.append(f'{identity}: deliberate absence has no reason')
                continue
            if not exposure.present:
                failures.append(f'{identity}: non-deliberate capability remains absent')
                continue
            if exposure.surface == 'CLI':
                if not exposure.names:
                    failures.append(f'{identity}: no declared command')
                for name in exposure.names:
                    if name not in cli_commands:
                        failures.append(f'{identity}: actual parser lacks {name}')
                    if name not in CLI_COMMAND_BINDINGS and name not in generated_cli_commands:
                        failures.append(f'{identity}: actual CLI dispatcher lacks {name}')
            elif exposure.surface == 'API':
                names = spendguard.CAPABILITY_API_COVERAGE.get(capability.id, ())
                if not names:
                    failures.append(f'{identity}: no actual API locator')
                for name in names:
                    if name not in spendguard.__all__ or not callable(getattr(spendguard, name, None)):
                        failures.append(f'{identity}: actual API export missing {name}')
            elif exposure.surface == 'MCP':
                names = mcp_server.CAPABILITY_MCP_COVERAGE.get(capability.id, ())
                if not names:
                    failures.append(f'{identity}: no actual MCP locator')
                for name in names:
                    entry = mcp_server._TOOLS.get(name)
                    if entry is None or not callable(entry[2]):
                        failures.append(f'{identity}: actual MCP dispatch missing {name}')
    assert not failures, '\n'.join(failures)


def verify_capability_parity_detects_dropped_export():
    """A locator left intact cannot conceal a removed actual surface callable."""
    name = next(name for names in spendguard.CAPABILITY_API_COVERAGE.values() for name in names)
    original = vars(spendguard).pop(name)
    try:
        try:
            assert_capability_actual_surface_parity()
        except AssertionError as error:
            assert f'actual API export missing {name}' in str(error), str(error)
        else:
            raise AssertionError('Parity guard accepted an actual API export removal')
    finally:
        setattr(spendguard, name, original)


if __name__ == '__main__':
    assert_capability_actual_surface_parity()
    verify_capability_parity_detects_dropped_export()
    print('Surface parity guard: actual CLI/API/MCP presence and dropped-export rejection pass')

