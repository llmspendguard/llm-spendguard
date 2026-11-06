"""Import the lazy spend-reporting homes so their accessor resolvers register."""


def wire_spend_reporting_accessor_resolvers() -> None:
    from . import receipt  # noqa: F401
    from . import report  # noqa: F401
