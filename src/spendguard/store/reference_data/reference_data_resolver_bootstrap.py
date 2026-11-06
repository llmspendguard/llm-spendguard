"""Import reference-data homes outside the SCC so their accessor resolvers register."""


def wire_reference_data_accessor_resolvers() -> None:
    from . import catalog, lane_catalog, reasoning_equivalence, sync_capabilities  # noqa: F401
