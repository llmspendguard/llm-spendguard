"""Routing seam keeps moved symbols reachable and its lazy wiring dependency-free."""

import ast
import importlib
from pathlib import Path


ROUTING_DIRECTORY = Path(__file__).resolve().parents[1] / "src/spendguard/manage/routing"
SPENDGUARD_SOURCE_DIRECTORY = ROUTING_DIRECTORY.parents[1]
ACCESSORS_NAME = "routing_accessors"
BOOTSTRAP_NAME = "routing_resolver_bootstrap"


def registered_accessor_bindings(home_tree):
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


bootstrap_tree = ast.parse((ROUTING_DIRECTORY / f"{BOOTSTRAP_NAME}.py").read_text())
loaded_home_names = {
    alias.name
    for node in ast.walk(bootstrap_tree)
    if isinstance(node, ast.FunctionDef) and node.name == "load_routing_accessor_home"
    for descendant in ast.walk(node)
    if isinstance(descendant, ast.ImportFrom) and descendant.level
    for alias in descendant.names
}
assert loaded_home_names, "routing bootstrap registered no homes"
accessors = importlib.import_module(f"spendguard.manage.routing.{ACCESSORS_NAME}")
symbols_by_home = {
    home_name: registered_accessor_bindings(
        ast.parse((ROUTING_DIRECTORY / f"{home_name}.py").read_text())
    )
    for home_name in loaded_home_names
}
assert all(symbols_by_home.values()), "a routing home registered no moved symbols"

registered_symbols = set().union(*symbols_by_home.values())
consumed_symbols = {
    node.func.attr.removeprefix("resolve_")
    for module_path in SPENDGUARD_SOURCE_DIRECTORY.rglob("*.py")
    for node in ast.walk(ast.parse(module_path.read_text()))
    if isinstance(node, ast.Call)
    and isinstance(node.func, ast.Attribute)
    and isinstance(node.func.value, ast.Name)
    and node.func.value.id == ACCESSORS_NAME
    and node.func.attr.startswith("resolve_")
}
assert consumed_symbols <= registered_symbols, "a consumed routing resolver has no home registration"

for home_name, moved_bindings in symbols_by_home.items():
    home = importlib.import_module(f"spendguard.manage.routing.{home_name}")
    moved_symbols = set(moved_bindings)
    assert not [name for name in sorted(moved_symbols) if not hasattr(home, name)]
    assert not [name for name in sorted(moved_symbols) if not hasattr(accessors, f"resolve_{name}")]
    assert not [
        name for name, producer_name in sorted(moved_bindings.items())
        if name not in accessors._routing_resolvers
        or (producer_name is not None and accessors._routing_resolvers[name] is not getattr(home, producer_name))
    ], f"routing resolver does not reach its registered producer in {home_name}"

    proof_symbol = next(iter(moved_symbols))
    proof_value = getattr(home, proof_symbol)
    delattr(home, proof_symbol)
    try:
        assert not hasattr(home, proof_symbol)
    finally:
        setattr(home, proof_symbol, proof_value)

accessors_tree = ast.parse((ROUTING_DIRECTORY / f"{ACCESSORS_NAME}.py").read_text())
assert not spendguard_internal_imports(ast.walk(accessors_tree))
assert not spendguard_internal_imports(bootstrap_tree.body)

planted_tree = ast.parse("from spendguard import config")
assert spendguard_internal_imports(planted_tree.body), "planted module-scope dependency was not rejected"
