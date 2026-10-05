"""Small shared mechanics for command-line argument lists."""


def cli_option_value_after(arguments, flag, default=None):
    """Return the value immediately after ``flag``, or ``default`` when absent/value-less."""
    index = arguments.index(flag) if flag in arguments else -1
    return arguments[index + 1] if 0 <= index < len(arguments) - 1 else default
