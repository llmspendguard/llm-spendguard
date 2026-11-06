"""Eagerly import lazy admission homes so their accessor resolvers register."""


def wire_admission_accessor_resolvers() -> None:
    from . import (  # noqa: F401
        model_preflight,
        plan_admission,
        residency_gate,
        weekly_cap_guard,
    )
