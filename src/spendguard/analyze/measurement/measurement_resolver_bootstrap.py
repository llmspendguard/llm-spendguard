"""Register deferred measurement home loading without importing the home."""


def wire_measurement_accessor_resolvers() -> None:
    from . import measurement_accessors

    def load_measurement_accessor_home():
        from . import bakeoff  # noqa: F401

    measurement_accessors.register_measurement_lazy_loader(load_measurement_accessor_home)
