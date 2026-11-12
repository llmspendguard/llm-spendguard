"""Compatibility shim for the SQLite-backed catalog accessor.

Callers keep importing this historical module name; all behavior and state live in
``catalog_store`` so there is one read path.
"""
import os
import sys
import types

from . import catalog_store as _catalog_store

# Retained as the sole legacy JSON path literal for callers that inspect or patch
# the historical authoring input. Runtime catalog reads are owned by catalog_store.
DATA_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "model_catalog.json")

for _catalog_name in dir(_catalog_store):
    if not _catalog_name.startswith("__"):
        globals()[_catalog_name] = getattr(_catalog_store, _catalog_name)

del _catalog_name


class _CatalogShimModule(types.ModuleType):
    """Forward compatibility patches to the single accessor implementation."""

    def __setattr__(self, name, value):
        super().__setattr__(name, value)
        if not name.startswith("__") and name not in {"_catalog_store", "DATA_PATH"}:
            setattr(_catalog_store, name, value)
        elif name == "DATA_PATH":
            setattr(_catalog_store, name, value)


sys.modules[__name__].__class__ = _CatalogShimModule
