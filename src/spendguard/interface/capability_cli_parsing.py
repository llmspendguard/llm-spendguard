"""Generate argparse arguments from capability registry CLI projections."""

from __future__ import annotations

from typing import Any


def registered_cli_option_token(token_id: str) -> str:
    """Return the registered spelling used by a legacy argv compatibility branch."""
    from .capability_registry import CLI_MANUAL_OPTION_PROJECTIONS

    return CLI_MANUAL_OPTION_PROJECTIONS[token_id]['spelling']


def add_registered_cli_argument(parser: Any, declaration_id: str, **dynamic_attributes: Any) -> Any:
    """Add a declared argument, preserving the legacy parser and evaluation site.

    Dynamic attributes are evaluated by the original handler. All literal names
    and attributes belong to the registry projection and have one declaration.
    """
    from .capability_registry import CLI_ARGUMENT_PROJECTIONS

    projection = CLI_ARGUMENT_PROJECTIONS[declaration_id]
    attributes = dict(projection.literal_attributes)
    required_dynamic = frozenset(projection.dynamic_attributes)
    supplied_dynamic = frozenset(dynamic_attributes)
    if supplied_dynamic != required_dynamic:
        raise ValueError(
            f"CLI projection {declaration_id}: expected dynamic attributes "
            f"{sorted(required_dynamic)}, got {sorted(supplied_dynamic)}"
        )
    attributes.update(dynamic_attributes)
    return parser.add_argument(*projection.names, **attributes)

