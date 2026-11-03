"""Phase 2c legacy-shim and published-export resolution proofs."""

import importlib
import json
from pathlib import Path
import sys
import tempfile
from types import ModuleType
import warnings

from spendguard.architectural_fitness import (
    ARCHITECTURAL_BOUNDARY_SHIM_KEY,
    load_architectural_context_registry,
)
from spendguard.domain_context_surface import install_domain_context_surface
from spendguard.domain_legacy_path_shim import (
    DomainLegacyPathAliasFinder,
    install_declared_domain_legacy_path_shims,
    install_domain_legacy_path_shim,
)


PHASE2C_REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
PHASE2C_REGISTRY_PATH = (
    PHASE2C_REPOSITORY_ROOT / "src" / "spendguard" / "domain_context_registry.json"
)
PHASE2C_FIXTURE_PACKAGE = "phase2c_compat_fixture"
PHASE2C_CURRENT_MODULE = f"{PHASE2C_FIXTURE_PACKAGE}.current_module"
PHASE2C_LEGACY_MODULE = f"{PHASE2C_FIXTURE_PACKAGE}.legacy_module"
PHASE2C_SHADOW_MODULE = f"{PHASE2C_FIXTURE_PACKAGE}.shadow_module"
PHASE2C_EXECUTION_COUNTER_NAME = "execution_count"
PHASE2C_SENTINEL_NAME = "sentinel"
PHASE2C_SURFACE_NAME = "published_sentinel"
PHASE2C_MISSING_ATTRIBUTE = "missing_sentinel"
PHASE2C_INITIAL_EXECUTION_COUNT = 0
PHASE2C_SINGLE_EXECUTION_COUNT = 1
PHASE2C_DECLARED_SHIM_COUNT = 65
PHASE2C_UNDECLARED_MODULE = f"{PHASE2C_FIXTURE_PACKAGE}.undeclared_module"


def write_phase2c_fixture_package(directory):
    fixture_package_path = Path(directory) / PHASE2C_FIXTURE_PACKAGE
    fixture_package_path.mkdir()
    (fixture_package_path / "__init__.py").write_text("")
    (fixture_package_path / "current_module.py").write_text(
        f"{PHASE2C_EXECUTION_COUNTER_NAME} = "
        f"globals().get({PHASE2C_EXECUTION_COUNTER_NAME!r}, "
        f"{PHASE2C_INITIAL_EXECUTION_COUNT}) + {PHASE2C_SINGLE_EXECUTION_COUNT}\n"
        f"{PHASE2C_SENTINEL_NAME} = object()\n"
    )


def clear_phase2c_fixture_modules():
    for module_name in tuple(sys.modules):
        if module_name == PHASE2C_FIXTURE_PACKAGE or module_name.startswith(
            PHASE2C_FIXTURE_PACKAGE + "."
        ):
            sys.modules.pop(module_name, None)


def assert_phase2c_same_module_identity(left_module, right_module):
    assert left_module is right_module


def test_phase2c_shim_identity_and_wrong_mechanism_red_proof():
    with tempfile.TemporaryDirectory() as directory:
        write_phase2c_fixture_package(directory)
        sys.path.insert(0, directory)
        try:
            installed_module = install_domain_legacy_path_shim(
                PHASE2C_LEGACY_MODULE, PHASE2C_CURRENT_MODULE
            )
            legacy_module = importlib.import_module(PHASE2C_LEGACY_MODULE)
            current_module = importlib.import_module(PHASE2C_CURRENT_MODULE)
            assert_phase2c_same_module_identity(legacy_module, current_module)
            imported_names = {}
            exec(
                f"from {PHASE2C_LEGACY_MODULE} import {PHASE2C_SENTINEL_NAME}",
                imported_names,
            )
            assert imported_names[PHASE2C_SENTINEL_NAME] is installed_module.sentinel
            assert installed_module.execution_count == PHASE2C_SINGLE_EXECUTION_COUNT

            divergent_copy = ModuleType("phase2c_divergent_copy")
            divergent_copy.__dict__.update(current_module.__dict__)
            try:
                assert_phase2c_same_module_identity(divergent_copy, current_module)
            except AssertionError:
                pass
            else:
                raise AssertionError("a from-import-star-style copy must fail module identity")
        finally:
            sys.path.remove(directory)
            clear_phase2c_fixture_modules()


def test_phase2c_shim_refuses_live_shadow():
    with tempfile.TemporaryDirectory() as directory:
        write_phase2c_fixture_package(directory)
        sys.path.insert(0, directory)
        try:
            sys.modules[PHASE2C_SHADOW_MODULE] = ModuleType(PHASE2C_SHADOW_MODULE)
            try:
                install_domain_legacy_path_shim(PHASE2C_SHADOW_MODULE, PHASE2C_CURRENT_MODULE)
            except RuntimeError:
                pass
            else:
                raise AssertionError("a different live legacy module must be refused")
        finally:
            sys.path.remove(directory)
            clear_phase2c_fixture_modules()


def test_phase2c_shim_installation_is_idempotent():
    with tempfile.TemporaryDirectory() as directory:
        write_phase2c_fixture_package(directory)
        sys.path.insert(0, directory)
        try:
            first_module = install_domain_legacy_path_shim(
                PHASE2C_LEGACY_MODULE, PHASE2C_CURRENT_MODULE
            )
            second_module = install_domain_legacy_path_shim(
                PHASE2C_LEGACY_MODULE, PHASE2C_CURRENT_MODULE
            )
            assert second_module is first_module
            assert second_module.execution_count == PHASE2C_SINGLE_EXECUTION_COUNT
        finally:
            sys.path.remove(directory)
            clear_phase2c_fixture_modules()


def test_phase3_lazy_finder_defers_execution_and_preserves_identity():
    with tempfile.TemporaryDirectory() as directory:
        write_phase2c_fixture_package(directory)
        sys.path.insert(0, directory)
        finder = DomainLegacyPathAliasFinder(
            {PHASE2C_LEGACY_MODULE: PHASE2C_CURRENT_MODULE}
        )
        sys.meta_path.insert(0, finder)
        try:
            assert finder.find_spec(PHASE2C_UNDECLARED_MODULE, None) is None
            assert PHASE2C_CURRENT_MODULE not in sys.modules
            with warnings.catch_warnings(record=True) as caught_warnings:
                warnings.simplefilter("always", DeprecationWarning)
                legacy_module = importlib.import_module(PHASE2C_LEGACY_MODULE)
            current_module = importlib.import_module(PHASE2C_CURRENT_MODULE)
            assert_phase2c_same_module_identity(legacy_module, current_module)
            assert current_module.execution_count == PHASE2C_SINGLE_EXECUTION_COUNT
            fixture_parent = importlib.import_module(PHASE2C_FIXTURE_PACKAGE)
            assert fixture_parent.legacy_module is current_module
            assert any(
                issubclass(warning.category, DeprecationWarning)
                for warning in caught_warnings
            )
        finally:
            sys.meta_path.remove(finder)
            sys.path.remove(directory)
            clear_phase2c_fixture_modules()


def test_phase2c_surface_exports_resolve_and_bad_attribute_goes_red():
    backing_module = ModuleType("phase2c_surface_backing")
    backing_sentinel = object()
    backing_module.sentinel = backing_sentinel
    sys.modules[backing_module.__name__] = backing_module
    try:
        surface_module = ModuleType("phase2c_surface")
        install_domain_context_surface(
            surface_module.__dict__,
            {PHASE2C_SURFACE_NAME: f"{backing_module.__name__}:{PHASE2C_SENTINEL_NAME}"},
        )
        assert surface_module.__all__ == sorted([PHASE2C_SURFACE_NAME])
        assert getattr(surface_module, PHASE2C_SURFACE_NAME) is backing_sentinel

        bad_surface_module = ModuleType("phase2c_bad_surface")
        install_domain_context_surface(
            bad_surface_module.__dict__,
            {PHASE2C_SURFACE_NAME: f"{backing_module.__name__}:{PHASE2C_MISSING_ATTRIBUTE}"},
        )
        try:
            getattr(bad_surface_module, PHASE2C_SURFACE_NAME)
        except AttributeError:
            pass
        else:
            raise AssertionError("a missing backing attribute must fail surface resolution")
    finally:
        sys.modules.pop(backing_module.__name__, None)


def test_phase2c_live_registry_drives_resolution_and_boundary_exemptions():
    registry = json.loads(PHASE2C_REGISTRY_PATH.read_text())
    declared_shims = registry["domain_legacy_path_shims"]
    install_declared_domain_legacy_path_shims(registry)
    for declaration in declared_shims:
        assert importlib.import_module(declaration["legacy_module"]) is importlib.import_module(
            declaration["current_module"]
        )
    derived_registry = load_architectural_context_registry(PHASE2C_REPOSITORY_ROOT)
    declared_legacy_modules = {item["legacy_module"] for item in declared_shims}
    assert set(derived_registry[ARCHITECTURAL_BOUNDARY_SHIM_KEY]) == declared_legacy_modules
    assert len(declared_shims) == PHASE2C_DECLARED_SHIM_COUNT


def test_live_context_published_names_resolve_and_are_public():
    registry = json.loads(PHASE2C_REGISTRY_PATH.read_text())
    for context in registry["contexts"]:
        surface = importlib.import_module(context["surface_module"])
        assert surface.__all__ == context["published_names"]
        for published_name in context["published_names"]:
            assert not published_name.startswith("_")
            getattr(surface, published_name)


test_phase2c_shim_identity_and_wrong_mechanism_red_proof()
test_phase2c_shim_refuses_live_shadow()
test_phase2c_shim_installation_is_idempotent()
test_phase3_lazy_finder_defers_execution_and_preserves_identity()
test_phase2c_surface_exports_resolve_and_bad_attribute_goes_red()
test_phase2c_live_registry_drives_resolution_and_boundary_exemptions()
test_live_context_published_names_resolve_and_are_public()
print(
    f"[OK] Phase 2c exports resolve across {PHASE2C_DECLARED_SHIM_COUNT} declared legacy shims"
)
