"""Generated console entry points bypass the compatibility warning shim."""
import importlib
import inspect
import pathlib
import sys
import warnings

import tomllib


PROJECT_ROOT = pathlib.Path(__file__).resolve().parent.parent
CANONICAL_CLI_MODULE = "spendguard.interface.cli"
LEGACY_CLI_MODULE = "spendguard.cli"


def _load_declared_console_callable():
    declaration = tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text())["project"]["scripts"]["spendguard"]
    module_name, callable_name = declaration.split(":", 1)
    return declaration, getattr(importlib.import_module(module_name), callable_name)


def test_console_entry_point_resolves_canonical_main_without_deprecation_warning():
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        declaration, declared_main = _load_declared_console_callable()
    canonical_main = importlib.import_module(CANONICAL_CLI_MODULE).main
    assert declaration == f"{CANONICAL_CLI_MODULE}:main"
    assert callable(declared_main)
    assert inspect.signature(declared_main) == inspect.signature(canonical_main)
    assert not any(issubclass(item.category, DeprecationWarning) for item in caught)


def test_legacy_cli_import_still_proves_warning_check_can_fail():
    sys.modules.pop(LEGACY_CLI_MODULE, None)
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        imported = importlib.import_module(LEGACY_CLI_MODULE)
    assert callable(imported.main)
    assert any(issubclass(item.category, DeprecationWarning) for item in caught)


test_console_entry_point_resolves_canonical_main_without_deprecation_warning()
test_legacy_cli_import_still_proves_warning_check_can_fail()
print("[OK] console entry point bypasses the deprecated compatibility shim")
