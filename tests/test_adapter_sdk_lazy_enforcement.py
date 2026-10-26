"""Offline guard for adapter SDK live post-import enforcement.

The mutation assertion deliberately removes the callback before the positive import path. That proves the
fail-closed probe can reject the exact missing-hook regression, rather than only observing a passing setup.
"""
import importlib
import importlib.abc
import importlib.machinery
import os
import sys
import tempfile
from types import ModuleType
from unittest import mock


ADAPTER_SDK_MODULE = "litellm"
ADAPTER_TEST_HOME_PREFIX = "spendguard-adapter-hook-"

if not os.environ.get("SPENDGUARD_TEST_ISOLATED"):
    os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"
    os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix=ADAPTER_TEST_HOME_PREFIX)
    os.execv(sys.executable, [sys.executable] + sys.argv)

from spendguard import gate, litellm_adapter  # noqa: E402


class AdapterSdkStubLoader(importlib.abc.Loader):
    def create_module(self, spec):
        return ModuleType(spec.name)

    def exec_module(self, module):
        module.success_callback = []


class AdapterSdkStubFinder(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname != ADAPTER_SDK_MODULE:
            return None
        return importlib.machinery.ModuleSpec(fullname, AdapterSdkStubLoader())


def test_adapter_sdk_live_hook_enforces_and_autowires():
    postimport_finder = gate._gate_package_import_finder
    original_meta_path = list(sys.meta_path)
    original_callbacks = {name: list(callbacks) for name, callbacks in postimport_finder.callbacks.items()}
    original_package_locks = dict(postimport_finder.package_locks)
    original_active_packages = set(postimport_finder.active_packages)
    original_sdk_module = sys.modules.pop(ADAPTER_SDK_MODULE, None)
    stub_finder = AdapterSdkStubFinder()
    try:
        with postimport_finder.lock:
            postimport_finder.callbacks.pop(ADAPTER_SDK_MODULE, None)
        sys.meta_path[:] = [postimport_finder, stub_finder] + [
            finder for finder in original_meta_path if finder is not postimport_finder
        ]
        with mock.patch.object(gate, "gated_sdk_modules", return_value=[ADAPTER_SDK_MODULE]):
            gate.install()
            assert gate._enforcement_armed(), "installed adapter SDK with a live hook was not accepted"

            with postimport_finder.lock:
                postimport_finder.callbacks[ADAPTER_SDK_MODULE].remove(gate._install_imported_adapter_sdk)
            assert not gate._enforcement_armed(), "mutation proof: missing adapter hook was accepted"

            gate._register_gate_package_postimport(ADAPTER_SDK_MODULE, gate._install_imported_adapter_sdk)
            imported_sdk = importlib.import_module(ADAPTER_SDK_MODULE)
            assert litellm_adapter.record_litellm in imported_sdk.success_callback
            assert gate._enforcement_armed(), "wired imported adapter SDK was not accepted"

            imported_sdk.success_callback.remove(litellm_adapter.record_litellm)
            assert not gate._enforcement_armed(), "imported-but-unwired adapter SDK was accepted"
    finally:
        sys.meta_path[:] = original_meta_path
        with postimport_finder.lock:
            postimport_finder.callbacks.clear()
            postimport_finder.callbacks.update(original_callbacks)
            postimport_finder.package_locks.clear()
            postimport_finder.package_locks.update(original_package_locks)
            postimport_finder.active_packages.clear()
            postimport_finder.active_packages.update(original_active_packages)
        sys.modules.pop(ADAPTER_SDK_MODULE, None)
        if original_sdk_module is not None:
            sys.modules[ADAPTER_SDK_MODULE] = original_sdk_module


test_adapter_sdk_live_hook_enforces_and_autowires()
print("OK test_adapter_sdk_lazy_enforcement")
