"""Platform seam homes retain their public surface and the leaf remains dependency-free."""

import ast
import importlib
from pathlib import Path


PLATFORM_DIRECTORY = Path(__file__).resolve().parents[1] / "src/spendguard/platform"
ACCESSOR_LEAF_NAME = "platform_accessors"
HOME_MODULE_EXCLUSIONS = {"__init__", ACCESSOR_LEAF_NAME, "platform_resolver_bootstrap"}


def imported_platform_leaf_symbols(home_tree):
    imported = set()
    for node in ast.walk(home_tree):
        if isinstance(node, ast.ImportFrom) and node.module == ACCESSOR_LEAF_NAME:
            imported.update(alias.asname or alias.name for alias in node.names)
    return imported


def registered_platform_resolver_symbols(home_tree):
    registered = set()
    for node in ast.walk(home_tree):
        if (not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute)
                or not isinstance(node.func.value, ast.Name)
                or node.func.value.id != "_platform_accessors"):
            continue
        setter_name = node.func.attr
        if setter_name.startswith("set_") and setter_name.endswith("_resolver"):
            registered.add(setter_name.removeprefix("set_").removesuffix("_resolver"))
    return registered


def mechanically_owned_platform_symbols():
    ownership = {}
    for path in sorted(PLATFORM_DIRECTORY.glob("*.py")):
        if path.stem in HOME_MODULE_EXCLUSIONS:
            continue
        home_tree = ast.parse(path.read_text())
        symbols = imported_platform_leaf_symbols(home_tree) | registered_platform_resolver_symbols(home_tree)
        if symbols:
            ownership[path.stem] = symbols
    return ownership


def missing_platform_home_reexports(ownership):
    missing = []
    for home_name, symbols in ownership.items():
        home = importlib.import_module(f"spendguard.platform.{home_name}")
        missing.extend(f"{home_name}.{symbol}" for symbol in sorted(symbols) if not hasattr(home, symbol))
    return missing


def spendguard_internal_imports(source_text):
    internal_imports = []
    for node in ast.walk(ast.parse(source_text)):
        if isinstance(node, ast.Import):
            internal_imports.extend(
                alias.name for alias in node.names
                if alias.name == "spendguard" or alias.name.startswith("spendguard.")
            )
        elif isinstance(node, ast.ImportFrom):
            module_name = node.module or ""
            if node.level or module_name == "spendguard" or module_name.startswith("spendguard."):
                internal_imports.append(module_name or ".")
    return internal_imports


ownership = mechanically_owned_platform_symbols()
assert ownership, "no moved platform symbols were discovered from leaf imports and resolver wiring"
assert not missing_platform_home_reexports(ownership), missing_platform_home_reexports(ownership)

proof_home_name = next(iter(ownership))
proof_symbol = next(iter(ownership[proof_home_name]))
proof_home = importlib.import_module(f"spendguard.platform.{proof_home_name}")
proof_value = getattr(proof_home, proof_symbol)
delattr(proof_home, proof_symbol)
try:
    assert f"{proof_home_name}.{proof_symbol}" in missing_platform_home_reexports(ownership)
finally:
    setattr(proof_home, proof_symbol, proof_value)

leaf_source = (PLATFORM_DIRECTORY / f"{ACCESSOR_LEAF_NAME}.py").read_text()
assert not spendguard_internal_imports(leaf_source), spendguard_internal_imports(leaf_source)
