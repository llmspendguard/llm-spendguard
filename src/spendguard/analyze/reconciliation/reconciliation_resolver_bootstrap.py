"""Register deferred reconciliation home loading without importing the home."""


def wire_reconciliation_accessor_resolvers() -> None:
    from . import reconciliation_accessors

    def load_reconciliation_accessor_home():
        from . import ledger_sync  # noqa: F401

    reconciliation_accessors.register_reconciliation_lazy_loader(load_reconciliation_accessor_home)
