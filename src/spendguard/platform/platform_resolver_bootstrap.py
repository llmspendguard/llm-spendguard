"""Eagerly import lazy platform homes so their accessor resolvers register."""


def wire_platform_accessor_resolvers() -> None:
    from . import distribution, distribution_daemon, release, setup  # noqa: F401
