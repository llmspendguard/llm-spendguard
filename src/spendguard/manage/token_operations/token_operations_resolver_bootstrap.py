"""Register deferred token-operations home loading without importing either home."""


def wire_token_operations_accessor_resolvers() -> None:
    from . import token_operations_accessors

    def load_token_operations_accessor_homes():
        from . import codex_daemon  # noqa: F401
        from . import codex_broker_owner  # noqa: F401
        from . import lane_queue  # noqa: F401

    token_operations_accessors.register_lazy_loader(load_token_operations_accessor_homes)
