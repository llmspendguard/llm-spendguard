"""Every symbol moved to execution_accessors remains public on its owning home."""
import ast
import importlib
import pathlib


EXECUTION_DIR = pathlib.Path(__file__).resolve().parents[1] / "src/spendguard/generate/execution"
LEAF_NAME = "execution_accessors"
HOME_EXCLUSIONS = {"__init__", LEAF_NAME}


def imported_leaf_symbols(home_tree):
    imported = set()
    for node in ast.walk(home_tree):
        if isinstance(node, ast.ImportFrom) and node.module == LEAF_NAME:
            imported.update(alias.asname or alias.name for alias in node.names)
    return imported


def resolver_symbols(home_tree):
    resolved = set()
    for node in ast.walk(home_tree):
        if (not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute)
                or not isinstance(node.func.value, ast.Name)
                or node.func.value.id != "_execution_accessors"):
            continue
        setter = node.func.attr
        if setter.startswith("set_") and setter.endswith("_resolver"):
            resolved.add(setter.removeprefix("set_").removesuffix("_resolver"))
    return resolved


def mechanically_owned_symbols():
    ownership = {}
    for path in sorted(EXECUTION_DIR.glob("*.py")):
        if path.stem in HOME_EXCLUSIONS:
            continue
        tree = ast.parse(path.read_text())
        for symbol in imported_leaf_symbols(tree) | resolver_symbols(tree):
            ownership.setdefault(path.stem, set()).add(symbol)
    return ownership


def missing_home_reexports(ownership):
    missing = []
    for home_name, symbols in ownership.items():
        home = importlib.import_module(f"spendguard.generate.execution.{home_name}")
        missing.extend(f"{home_name}.{symbol}" for symbol in sorted(symbols) if not hasattr(home, symbol))
    return missing


ownership = mechanically_owned_symbols()
assert ownership, "no moved execution symbols were discovered from leaf imports and resolver wiring"
assert not missing_home_reexports(ownership), missing_home_reexports(ownership)

# Anti-vacuity: removing any discovered re-export is observed by the same check.
proof_home_name = next(iter(ownership))
proof_symbol = next(iter(ownership[proof_home_name]))
proof_home = importlib.import_module(f"spendguard.generate.execution.{proof_home_name}")
proof_value = getattr(proof_home, proof_symbol)
delattr(proof_home, proof_symbol)
try:
    assert f"{proof_home_name}.{proof_symbol}" in missing_home_reexports(ownership)
finally:
    setattr(proof_home, proof_symbol, proof_value)

print(f"GREEN execution home re-exports: {sum(map(len, ownership.values()))} moved symbols; deletion proof: REJECTED")
