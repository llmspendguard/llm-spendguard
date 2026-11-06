"""The public package root must remain outside every import-cycle component."""

from pathlib import Path

import pytest

from spendguard.architectural_fitness import (
    collapse_architectural_shim_edges,
    collect_architectural_import_edges,
    collect_architectural_source_units,
    load_architectural_context_registry,
    load_architectural_fitness_baseline,
    measure_architectural_cycle_magnitude,
)
from spendguard.analyze.assurance import architectural_fitness as architectural_fitness_implementation


ROOT_INBOUND_FIXTURE_SOURCE = "fixture.cyclic_consumer"


def assert_root_package_has_no_cyclic_inbound(cyclic_edges, package):
    inbound = sorted(edge for edge in cyclic_edges if edge[1] == package)
    assert not inbound, f"cyclic imports into public package root {package}: {inbound}"


def test_live_root_package_has_no_cyclic_inbound():
    repository = Path(__file__).resolve().parents[1]
    settings = load_architectural_fitness_baseline(repository)
    units = collect_architectural_source_units(repository, settings)
    registry = load_architectural_context_registry(repository)
    shim_map = {
        declaration[architectural_fitness_implementation.ARCHITECTURAL_LEGACY_SHIM_MODULE_KEY]:
            declaration[architectural_fitness_implementation.ARCHITECTURAL_CURRENT_SHIM_MODULE_KEY]
        for declaration in registry.get(
            architectural_fitness_implementation.ARCHITECTURAL_LEGACY_SHIM_REGISTRY_KEY, ()
        )
    }
    edges = collect_architectural_import_edges(units, legacy_modules=shim_map)
    magnitude = measure_architectural_cycle_magnitude(collapse_architectural_shim_edges(edges, shim_map))

    assert_root_package_has_no_cyclic_inbound(magnitude["cyclic_edges"], settings["package"])


def test_root_package_cyclic_inbound_guard_rejects_planted_edge():
    package = load_architectural_fitness_baseline(Path(__file__).resolve().parents[1])["package"]
    planted_edges = {(ROOT_INBOUND_FIXTURE_SOURCE, package)}

    with pytest.raises(AssertionError, match="cyclic imports into public package root"):
        assert_root_package_has_no_cyclic_inbound(planted_edges, package)


if __name__ == "__main__":
    test_live_root_package_has_no_cyclic_inbound()
    test_root_package_cyclic_inbound_guard_rejects_planted_edge()
    print("root package cyclic inbound: LIVE GREEN / PLANTED EDGE RED")
