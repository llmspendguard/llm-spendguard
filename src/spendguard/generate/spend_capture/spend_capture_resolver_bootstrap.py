"""Register deferred spend-capture home loading without importing the home."""


def wire_spend_capture_accessor_resolvers() -> None:
    from . import spend_capture_accessors

    def load_spend_capture_accessor_home():
        from . import chat  # noqa: F401

    spend_capture_accessors.register_spend_capture_lazy_loader(load_spend_capture_accessor_home)
