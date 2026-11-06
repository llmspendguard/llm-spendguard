"""Import the lazy SaaS home so its accessor resolvers register."""


def wire_egress_accessor_resolvers() -> None:
    from . import saas  # noqa: F401
