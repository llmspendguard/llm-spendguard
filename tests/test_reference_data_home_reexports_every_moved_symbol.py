"""Reference-data seam keeps every moved callable reachable and its leaves dependency-free."""

import ast
import importlib
from pathlib import Path


REFERENCE_DATA_DIRECTORY = Path(__file__).resolve().parents[1] / "src/spendguard/store/reference_data"
HOME_NAMES = ("lane_catalog", "catalog", "reasoning_equivalence", "sync_capabilities")
ACCESSORS_NAME = "reference_data_accessors"
BOOTSTRAP_NAME = "reference_data_resolver_bootstrap"


def registered_accessor_symbols(home_tree):
    registered = set()
    for node in ast.walk(home_tree):
        if (not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute)
                or not isinstance(node.func.value, ast.Name)
                or node.func.value.id != "_reference_data_accessors"):
            continue
        setter_name = node.func.attr
        if setter_name.startswith("set_") and setter_name.endswith("_resolver"):
            registered.add(setter_name.removeprefix("set_").removesuffix("_resolver"))
    return registered


def spendguard_internal_imports(nodes):
    internal_imports = []
    for node in nodes:
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


home_symbols = {}
for home_name in HOME_NAMES:
    home_tree = ast.parse((REFERENCE_DATA_DIRECTORY / f"{home_name}.py").read_text())
    home_symbols[home_name] = registered_accessor_symbols(home_tree)

moved_symbols = set().union(*home_symbols.values())
assert moved_symbols, "no moved reference-data callables were discovered"

accessors = importlib.import_module(f"spendguard.store.reference_data.{ACCESSORS_NAME}")
for home_name, symbols in home_symbols.items():
    home = importlib.import_module(f"spendguard.store.reference_data.{home_name}")
    assert not [name for name in sorted(symbols) if not hasattr(home, name)]
    assert not [name for name in sorted(symbols) if not hasattr(accessors, f"resolve_{name}")]

proof_home_name = next(name for name, symbols in home_symbols.items() if symbols)
proof_symbol = next(iter(home_symbols[proof_home_name]))
proof_home = importlib.import_module(f"spendguard.store.reference_data.{proof_home_name}")
proof_value = getattr(proof_home, proof_symbol)
delattr(proof_home, proof_symbol)
try:
    assert not hasattr(proof_home, proof_symbol)
finally:
    setattr(proof_home, proof_symbol, proof_value)

accessors_tree = ast.parse((REFERENCE_DATA_DIRECTORY / f"{ACCESSORS_NAME}.py").read_text())
assert not spendguard_internal_imports(ast.walk(accessors_tree))
bootstrap_tree = ast.parse((REFERENCE_DATA_DIRECTORY / f"{BOOTSTRAP_NAME}.py").read_text())
assert not spendguard_internal_imports(bootstrap_tree.body)

planted_tree = ast.parse("from spendguard import config")
assert spendguard_internal_imports(planted_tree.body), "planted module-scope dependency was not rejected"
