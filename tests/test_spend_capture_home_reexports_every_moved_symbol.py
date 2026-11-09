"""Spend-capture seam keeps moved symbols reachable and its lazy wiring dependency-free."""

import ast
import importlib
import inspect
from pathlib import Path


SPEND_CAPTURE_DIRECTORY = Path(__file__).resolve().parents[1] / "src/spendguard/generate/spend_capture"
ACCESSORS_NAME = "spend_capture_accessors"
BOOTSTRAP_NAME = "spend_capture_resolver_bootstrap"


def registered_spend_capture_accessor_bindings(home_tree):
    accessor_aliases = {
        alias.asname or alias.name
        for node in home_tree.body
        if isinstance(node, ast.ImportFrom) and node.level
        for alias in node.names
        if alias.name == ACCESSORS_NAME
    }
    registered = {}
    for statement in home_tree.body:
        node = statement.value if isinstance(statement, ast.Expr) else statement
        if (not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute)
                or not isinstance(node.func.value, ast.Name)
                or node.func.value.id not in accessor_aliases):
            continue
        setter_name = node.func.attr
        if setter_name.startswith("set_") and setter_name.endswith("_resolver"):
            symbol_name = setter_name.removeprefix("set_").removesuffix("_resolver")
            producer = node.args[0] if node.args else None
            registered[symbol_name] = producer.id if isinstance(producer, ast.Name) else None
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


bootstrap_tree = ast.parse((SPEND_CAPTURE_DIRECTORY / f"{BOOTSTRAP_NAME}.py").read_text())
loaded_home_names = {
    alias.name
    for node in ast.walk(bootstrap_tree)
    if isinstance(node, ast.FunctionDef) and node.name == "load_spend_capture_accessor_home"
    for descendant in ast.walk(node)
    if isinstance(descendant, ast.ImportFrom) and descendant.level
    for alias in descendant.names
}
assert loaded_home_names, "spend-capture bootstrap registered no homes"

module_trees = {
    module_path.stem: ast.parse(module_path.read_text())
    for module_path in SPEND_CAPTURE_DIRECTORY.glob("*.py")
}
symbols_by_home = {
    home_name: registered_spend_capture_accessor_bindings(module_trees[home_name])
    for home_name in loaded_home_names
}
assert all(symbols_by_home.values()), "a spend-capture home registered no moved symbols"

registered_symbols = set().union(*symbols_by_home.values())
consumed_symbols = {
    node.func.attr.removeprefix("resolve_")
    for module_tree in module_trees.values()
    for node in ast.walk(module_tree)
    if isinstance(node, ast.Call)
    and isinstance(node.func, ast.Attribute)
    and isinstance(node.func.value, ast.Name)
    and node.func.value.id == ACCESSORS_NAME
    and node.func.attr.startswith("resolve_")
}
assert consumed_symbols <= registered_symbols, "a consumed spend-capture resolver has no home registration"

accessors = importlib.import_module(f"spendguard.generate.spend_capture.{ACCESSORS_NAME}")
for home_name, moved_bindings in symbols_by_home.items():
    home = importlib.import_module(f"spendguard.generate.spend_capture.{home_name}")
    moved_symbols = set(moved_bindings)
    assert not [name for name in sorted(moved_symbols) if not hasattr(home, name)]
    assert not [name for name in sorted(moved_symbols) if not hasattr(accessors, f"resolve_{name}")]
    assert not [
        name for name, producer_name in sorted(moved_bindings.items())
        if name not in accessors._spend_capture_resolvers
        or (producer_name is not None
            and inspect.unwrap(accessors._spend_capture_resolvers[name]) is not getattr(home, producer_name))
    ], f"spend-capture resolver does not reach its registered producer in {home_name}"

    proof_symbol = next(iter(moved_symbols))
    proof_value = getattr(home, proof_symbol)
    delattr(home, proof_symbol)
    try:
        assert not hasattr(home, proof_symbol)
    finally:
        setattr(home, proof_symbol, proof_value)

accessors_tree = ast.parse((SPEND_CAPTURE_DIRECTORY / f"{ACCESSORS_NAME}.py").read_text())
assert not spendguard_internal_imports(ast.walk(accessors_tree))
assert not spendguard_internal_imports(bootstrap_tree.body)

planted_tree = ast.parse("from spendguard import config")
assert spendguard_internal_imports(planted_tree.body), "planted module-scope dependency was not rejected"
