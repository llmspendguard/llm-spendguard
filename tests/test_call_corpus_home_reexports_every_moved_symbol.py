"""Call-corpus seam keeps every moved callable reachable and its leaves dependency-free."""

import ast
import importlib
from pathlib import Path


CALL_CORPUS_DIRECTORY = Path(__file__).resolve().parents[1] / "src/spendguard/store/call_corpus"
HOME_NAME = "callio"
ACCESSORS_NAME = "call_corpus_accessors"
BOOTSTRAP_NAME = "call_corpus_resolver_bootstrap"


def registered_accessor_symbols(home_tree):
    registered = set()
    for node in ast.walk(home_tree):
        if (not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute)
                or not isinstance(node.func.value, ast.Name)
                or node.func.value.id != "_call_corpus_accessors"):
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


home_tree = ast.parse((CALL_CORPUS_DIRECTORY / f"{HOME_NAME}.py").read_text())
moved_symbols = registered_accessor_symbols(home_tree)
assert moved_symbols, "no moved call-corpus callables were discovered"

accessors = importlib.import_module(f"spendguard.store.call_corpus.{ACCESSORS_NAME}")
home = importlib.import_module(f"spendguard.store.call_corpus.{HOME_NAME}")
assert not [name for name in sorted(moved_symbols) if not hasattr(home, name)]
assert not [name for name in sorted(moved_symbols) if not hasattr(accessors, f"resolve_{name}")]

proof_symbol = next(iter(moved_symbols))
proof_value = getattr(home, proof_symbol)
delattr(home, proof_symbol)
try:
    assert not hasattr(home, proof_symbol)
finally:
    setattr(home, proof_symbol, proof_value)

accessors_tree = ast.parse((CALL_CORPUS_DIRECTORY / f"{ACCESSORS_NAME}.py").read_text())
assert not spendguard_internal_imports(ast.walk(accessors_tree))
bootstrap_tree = ast.parse((CALL_CORPUS_DIRECTORY / f"{BOOTSTRAP_NAME}.py").read_text())
assert not spendguard_internal_imports(bootstrap_tree.body)

planted_tree = ast.parse("from spendguard import config")
assert spendguard_internal_imports(planted_tree.body), "planted module-scope dependency was not rejected"
