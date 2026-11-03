"""Preserve legacy module identity after domain modules move."""

import importlib
import importlib.abc
import importlib.util
import json
from pathlib import Path
import sys
import warnings


DOMAIN_LEGACY_SHIM_REGISTRY_KEY = "domain_legacy_path_shims"
DOMAIN_LEGACY_MODULE_KEY = "legacy_module"
DOMAIN_CURRENT_MODULE_KEY = "current_module"
DOMAIN_LEGACY_WARNING_STACKLEVEL = 2
DOMAIN_CONTEXT_REGISTRY_FILENAME = "domain_context_registry.json"


class DomainLegacyPathAliasLoader(importlib.abc.Loader):
    """Resolve one legacy path to its current module without executing it twice."""

    def __init__(self, legacy_dotted, current_dotted):
        self.legacy_dotted = legacy_dotted
        self.current_dotted = current_dotted
        self.current_module_spec = None

    def create_module(self, spec):
        current_module = importlib.import_module(self.current_dotted)
        self.current_module_spec = current_module.__spec__
        return current_module

    def exec_module(self, module):
        legacy_parent_dotted, _, legacy_leaf = self.legacy_dotted.rpartition(".")
        legacy_parent = importlib.import_module(legacy_parent_dotted)
        sys.modules[self.legacy_dotted] = module
        setattr(legacy_parent, legacy_leaf, module)
        if self.current_module_spec is not None:
            module.__spec__ = self.current_module_spec
            module.__loader__ = self.current_module_spec.loader
            module.__package__ = self.current_module_spec.parent
            module.__name__ = self.current_module_spec.name
        warnings.warn(
            f"{self.legacy_dotted} is deprecated; import {self.current_dotted} instead",
            DeprecationWarning,
            stacklevel=DOMAIN_LEGACY_WARNING_STACKLEVEL,
        )


class DomainLegacyPathAliasFinder(importlib.abc.MetaPathFinder):
    """Find only registry-declared legacy module paths and alias them lazily."""

    def __init__(self, legacy_to_current):
        self.legacy_to_current = dict(legacy_to_current)

    def find_spec(self, fullname, path=None, target=None):
        current_dotted = self.legacy_to_current.get(fullname)
        if current_dotted is None:
            return None
        loader = DomainLegacyPathAliasLoader(fullname, current_dotted)
        return importlib.util.spec_from_loader(fullname, loader)


def install_packaged_domain_legacy_path_finder():
    """Register the packaged legacy-path declarations without importing a moved module."""
    registry_path = Path(__file__).with_name(DOMAIN_CONTEXT_REGISTRY_FILENAME)
    registry = json.loads(registry_path.read_text())
    legacy_to_current = {
        declaration[DOMAIN_LEGACY_MODULE_KEY]: declaration[DOMAIN_CURRENT_MODULE_KEY]
        for declaration in registry.get(DOMAIN_LEGACY_SHIM_REGISTRY_KEY, ())
    }
    for finder in sys.meta_path:
        if isinstance(finder, DomainLegacyPathAliasFinder):
            if finder.legacy_to_current != legacy_to_current:
                raise RuntimeError("installed legacy-path finder disagrees with the packaged registry")
            return finder
    finder = DomainLegacyPathAliasFinder(legacy_to_current)
    sys.meta_path.insert(0, finder)
    return finder


def install_domain_legacy_path_shim(legacy_dotted, current_dotted):
    """Alias one legacy dotted path to its current module without re-execution."""
    current_module = importlib.import_module(current_dotted)
    live_legacy_module = sys.modules.get(legacy_dotted)
    if live_legacy_module is not None and live_legacy_module is not current_module:
        raise RuntimeError(
            f"refusing to shadow live legacy module {legacy_dotted!r} with {current_dotted!r}"
        )

    legacy_parent_dotted, separator, legacy_leaf = legacy_dotted.rpartition(".")
    if not separator:
        raise ValueError(f"legacy module path must have a parent package: {legacy_dotted!r}")
    legacy_parent = importlib.import_module(legacy_parent_dotted)
    live_parent_attribute = getattr(legacy_parent, legacy_leaf, None)
    if live_parent_attribute is not None and live_parent_attribute is not current_module:
        raise RuntimeError(
            f"refusing to shadow live legacy package attribute {legacy_dotted!r}"
        )

    sys.modules[legacy_dotted] = current_module
    setattr(legacy_parent, legacy_leaf, current_module)
    warnings.warn(
        f"{legacy_dotted} is deprecated; import {current_dotted} instead",
        DeprecationWarning,
        stacklevel=DOMAIN_LEGACY_WARNING_STACKLEVEL,
    )
    return current_module


def install_declared_domain_legacy_path_shims(registry):
    """Install every legacy-path alias declared in the domain registry."""
    return [
        install_domain_legacy_path_shim(
            declaration[DOMAIN_LEGACY_MODULE_KEY],
            declaration[DOMAIN_CURRENT_MODULE_KEY],
        )
        for declaration in registry.get(DOMAIN_LEGACY_SHIM_REGISTRY_KEY, ())
    ]
