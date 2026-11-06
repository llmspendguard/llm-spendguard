"""Spend-search seam surfaces retain identity, reachability, and dependency freedom."""

import ast
import importlib
from pathlib import Path


SPEND_SEARCH_DIRECTORY = Path(__file__).resolve().parents[1] / "src/spendguard/store/spend_search"
HOME_NAME = "ledger_read"
CONTRACTS_NAME = "ledger_read_contracts"
ACCESSORS_NAME = "ledger_read_accessors"


def imported_contract_symbols(home_tree):
    imported = set()
    for node in ast.walk(home_tree):
        if isinstance(node, ast.ImportFrom) and node.module == CONTRACTS_NAME:
            imported.update(alias.asname or alias.name for alias in node.names)
    return {name for name in imported if not name.startswith("set_")}


def registered_accessor_symbols(home_tree):
    registered = set()
    for node in ast.walk(home_tree):
        if (not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute)
                or not isinstance(node.func.value, ast.Name)
                or node.func.value.id != "_ledger_read_accessors"):
            continue
        setter_name = node.func.attr
        if setter_name.startswith("set_") and setter_name.endswith("_resolver"):
            registered.add(setter_name.removeprefix("set_").removesuffix("_resolver"))
    return registered


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


home_tree = ast.parse((SPEND_SEARCH_DIRECTORY / f"{HOME_NAME}.py").read_text())
contract_symbols = imported_contract_symbols(home_tree)
accessor_symbols = registered_accessor_symbols(home_tree)
config_resolver_symbols = {name for name in accessor_symbols if name.startswith("default_ledger_read_")}
callable_symbols = accessor_symbols - config_resolver_symbols
assert contract_symbols, "no moved spend-search identity symbols were discovered"
assert accessor_symbols, "no moved spend-search callable symbols were discovered"

home = importlib.import_module(f"spendguard.store.spend_search.{HOME_NAME}")
contracts = importlib.import_module(f"spendguard.store.spend_search.{CONTRACTS_NAME}")
accessors = importlib.import_module(f"spendguard.store.spend_search.{ACCESSORS_NAME}")
assert not [name for name in sorted(contract_symbols) if getattr(home, name) is not getattr(contracts, name)]
assert not [name for name in sorted(callable_symbols) if not hasattr(home, name)]
assert not [name for name in sorted(accessor_symbols) if not hasattr(accessors, f"resolve_{name}")]

proof_symbol = next(iter(contract_symbols))
proof_value = getattr(home, proof_symbol)
delattr(home, proof_symbol)
try:
    assert not hasattr(home, proof_symbol)
finally:
    setattr(home, proof_symbol, proof_value)

for leaf_name in (CONTRACTS_NAME, ACCESSORS_NAME):
    leaf_source = (SPEND_SEARCH_DIRECTORY / f"{leaf_name}.py").read_text()
    assert not spendguard_internal_imports(leaf_source), spendguard_internal_imports(leaf_source)
