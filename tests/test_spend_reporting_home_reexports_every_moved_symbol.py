"""Spend-reporting seam keeps every moved callable reachable and its leaves dependency-free."""

import ast
import importlib
from pathlib import Path


SPEND_REPORTING_DIRECTORY = Path(__file__).resolve().parents[1] / "src/spendguard/analyze/spend_reporting"
HOME_NAMES = ("receipt_tally", "report")
ACCESSORS_NAME = "spend_reporting_accessors"
BOOTSTRAP_NAME = "spend_reporting_resolver_bootstrap"


def registered_accessor_symbols(home_tree):
    registered = set()
    for node in ast.walk(home_tree):
        if (not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute)
                or not isinstance(node.func.value, ast.Name)
                or node.func.value.id != "_spend_reporting_accessors"):
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


home_trees = {
    home_name: ast.parse((SPEND_REPORTING_DIRECTORY / f"{home_name}.py").read_text())
    for home_name in HOME_NAMES
}
symbols_by_home = {
    home_name: registered_accessor_symbols(home_tree)
    for home_name, home_tree in home_trees.items()
}
assert all(symbols_by_home.values()), "a spend-reporting home registered no moved callables"
assert sum(map(len, symbols_by_home.values())) == len(set().union(*symbols_by_home.values())), (
    "moved spend-reporting callable names collide across homes")

accessors = importlib.import_module(f"spendguard.analyze.spend_reporting.{ACCESSORS_NAME}")
for home_name, moved_symbols in symbols_by_home.items():
    home = importlib.import_module(f"spendguard.analyze.spend_reporting.{home_name}")
    assert not [name for name in sorted(moved_symbols) if not hasattr(home, name)]
    assert not [name for name in sorted(moved_symbols) if not hasattr(accessors, f"resolve_{name}")]

    proof_symbol = next(iter(moved_symbols))
    proof_value = getattr(home, proof_symbol)
    delattr(home, proof_symbol)
    try:
        assert not hasattr(home, proof_symbol)
    finally:
        setattr(home, proof_symbol, proof_value)

accessors_tree = ast.parse((SPEND_REPORTING_DIRECTORY / f"{ACCESSORS_NAME}.py").read_text())
assert not spendguard_internal_imports(ast.walk(accessors_tree))
bootstrap_tree = ast.parse((SPEND_REPORTING_DIRECTORY / f"{BOOTSTRAP_NAME}.py").read_text())
assert not spendguard_internal_imports(bootstrap_tree.body)

planted_tree = ast.parse("from spendguard import config")
assert spendguard_internal_imports(planted_tree.body), "planted module-scope dependency was not rejected"
