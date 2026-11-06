"""Register deferred routing home loading without importing the home."""


def wire_routing_accessor_resolvers() -> None:
    from . import routing_accessors

    def load_routing_accessor_home():
        from . import lane_balance  # noqa: F401

    routing_accessors.register_routing_lazy_loader(load_routing_accessor_home)
