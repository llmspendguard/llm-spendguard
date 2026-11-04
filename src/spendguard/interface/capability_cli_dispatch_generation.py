"""Dispatch registered commands to existing owning callables lazily."""

from importlib import import_module
import sys


def invoke_registered_cli_command(command, rest, argv):
    """Preserve each registered handler's legacy argument and exit contract."""
    from .capability_registry import CLI_COMMAND_BINDINGS

    binding = CLI_COMMAND_BINDINGS.get(command)
    if binding is None:
        from .capability_cli_callable_generation import invoke_callable_cli_command

        return invoke_callable_cli_command(command, rest)
    module_name, qualified_name = binding.handler_ref.split(':', 1)
    handler = import_module(module_name)
    for component in qualified_name.split('.'):
        handler = getattr(handler, component)
    if binding.legacy_argv_prefix is not None:
        sys.argv = [binding.legacy_argv_prefix] + rest
    if binding.invocation_policy == 'rest':
        result = handler(rest)
    elif binding.invocation_policy == 'none':
        result = handler()
    elif binding.invocation_policy == 'command_prefix':
        result = handler([command] + rest)
    elif binding.invocation_policy == 'context':
        result = handler(rest, command, argv)
    else:
        raise ValueError(f'Unknown CLI invocation policy: {binding.invocation_policy}')
    return True, result
