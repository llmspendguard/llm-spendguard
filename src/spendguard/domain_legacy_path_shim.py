"""Preserve legacy module identity after domain modules move."""

import importlib
import sys
import warnings


DOMAIN_LEGACY_SHIM_REGISTRY_KEY = "domain_legacy_path_shims"
DOMAIN_LEGACY_MODULE_KEY = "legacy_module"
DOMAIN_CURRENT_MODULE_KEY = "current_module"
DOMAIN_LEGACY_WARNING_STACKLEVEL = 2


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
