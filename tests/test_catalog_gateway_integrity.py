"""Offline structural and transactional proofs for the one model-catalog port."""
import ast
import copy
import importlib.util
import itertools
from pathlib import Path

import pytest

from spendguard import catalog_price_authoring, model_catalog, sync_capabilities

GATEWAY_REPO_ROOT = Path(__file__).resolve().parents[1]
GATEWAY_SOURCE_ROOT = GATEWAY_REPO_ROOT / "src" / "spendguard"
GATEWAY_MODULE_PATH = Path(model_catalog.__file__).resolve()
GATEWAY_RESOURCE_NAMES = frozenset(("model_catalog.json", "catalog_seed.sql", "catalog.sqlite"))
GATEWAY_OLD_HANDLES = frozenset(("DATA_PATH", "CATALOG_DATABASE_PATH", "SHIPPED_CATALOG_SEED_PATH"))
GATEWAY_ACCESS_METHODS = frozenset((
    "open", "read_text", "read_bytes", "write_text", "write_bytes", "update_json",
    "connect", "executescript", "deserialize", "replace", "rename", "unlink", "remove",
))
GATEWAY_TEST_REASON = "offline transactional catalog proof"
GATEWAY_PRICE_SOURCE = "offline provenance fixture"
GATEWAY_PRICE_DELTA = 1
GATEWAY_INTERVAL_VALID_FROM = "2026-01-01"
GATEWAY_INTERVAL_VALID_UNTIL = "2026-02-01"
GATEWAY_UNKNOWN_MODEL_ID = "catalog-model-behavior-unknown-fixture"
GATEWAY_MUTATED_CAPABILITY_MODE = "mutated-test-value"
GATEWAY_BEHAVIOR_FIELDS = frozenset(
    ("output_ceiling", "context_window", "reasoning", "vision", "capabilities"))


def catalog_ast_lexical_bindings(tree):
    """Index actual Python lexical bindings instead of conflating equal names."""
    node_scopes = {}
    scope_parents = {tree: None}
    scope_bindings = {tree: set()}
    scope_globals = {tree: set()}
    scope_nonlocals = {tree: set()}

    def catalog_ast_index_scopes(node, scope):
        node_scopes[node] = scope
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            scope_bindings[scope].add(node.name)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef,
                             ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)):
            scope_parents[node] = scope
            scope_bindings[node] = set()
            scope_globals[node] = set()
            scope_nonlocals[node] = set()
            for child in ast.iter_child_nodes(node):
                catalog_ast_index_scopes(child, node)
            return
        if isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Param)):
            scope_bindings[scope].add(node.id)
        elif isinstance(node, ast.arg):
            scope_bindings[scope].add(node.arg)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            scope_bindings[scope].update(alias.asname or alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.Global):
            scope_globals[scope].update(node.names)
        elif isinstance(node, ast.Nonlocal):
            scope_nonlocals[scope].update(node.names)
        for child in ast.iter_child_nodes(node):
            catalog_ast_index_scopes(child, scope)

    catalog_ast_index_scopes(tree, tree)

    def catalog_ast_resolve_binding(name, scope):
        if name in scope_globals[scope]:
            return tree, name
        current = scope_parents[scope] if name in scope_nonlocals[scope] else scope
        while current is not None:
            if name in scope_bindings[current]:
                return current, name
            current = scope_parents[current]
            # A method closes over outer function scopes, never a class namespace.
            if isinstance(current, ast.ClassDef):
                current = scope_parents[current]
        return tree, name

    return node_scopes, catalog_ast_resolve_binding


def catalog_ast_access_violations(source, *, permitted=False):
    """Track resource expressions through AST bindings, calls, returns and aliases.

    This is syntactic data flow, not a semantic classifier. Every source node is
    visited, including nested functions and branches. Imported path handles and
    split constant path constructions are covered along with direct literals.
    """
    tree = ast.parse(source)
    nodes = list(ast.walk(tree))
    node_scopes, resolve_binding = catalog_ast_lexical_bindings(tree)
    tainted_bindings = set()
    call_aliases = {}
    literal_bindings = {}
    functions = {resolve_binding(node.name, node_scopes[node]): node for node in nodes
                 if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}

    def catalog_ast_binding(node):
        return resolve_binding(node.id, node_scopes[node])

    def catalog_ast_dotted_name(node):
        if isinstance(node, ast.Name):
            return call_aliases.get(catalog_ast_binding(node), node.id)
        if isinstance(node, ast.Attribute):
            return catalog_ast_dotted_name(node.value) + "." + node.attr
        return ""

    def catalog_ast_literal_text(node, active_bindings=frozenset()):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return frozenset((node.value,))
        if isinstance(node, ast.Name):
            binding = catalog_ast_binding(node)
            if binding in active_bindings:
                return frozenset()
            return frozenset(value for expression in literal_bindings.get(binding, ())
                             for value in catalog_ast_literal_text(expression, active_bindings | {binding}))
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            return frozenset(left + right for left, right in itertools.product(
                catalog_ast_literal_text(node.left, active_bindings),
                catalog_ast_literal_text(node.right, active_bindings)))
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
            return frozenset(str(Path(left) / right) for left, right in itertools.product(
                catalog_ast_literal_text(node.left, active_bindings),
                catalog_ast_literal_text(node.right, active_bindings)))
        if isinstance(node, ast.JoinedStr):
            return frozenset("".join(parts) for parts in itertools.product(*(
                catalog_ast_literal_text(part, active_bindings) for part in node.values)))
        if isinstance(node, ast.FormattedValue):
            return catalog_ast_literal_text(node.value, active_bindings)
        if isinstance(node, ast.Call):
            method = catalog_ast_dotted_name(node.func).rsplit(".", 1)[-1]
            arguments = [catalog_ast_literal_text(argument, active_bindings) for argument in node.args]
            if method in ("Path", "PurePath", "join", "joinpath") and arguments:
                if isinstance(node.func, ast.Attribute) and method == "joinpath":
                    arguments.insert(0, catalog_ast_literal_text(node.func.value, active_bindings))
                return frozenset(str(Path(*parts)) for parts in itertools.product(*arguments))
            if method == "with_suffix" and isinstance(node.func, ast.Attribute) and arguments:
                return frozenset(str(Path(base).with_suffix(suffix)) for base, suffix in itertools.product(
                    catalog_ast_literal_text(node.func.value, active_bindings), arguments[0]))
        return frozenset()

    def catalog_ast_has_resource(node):
        if node is None:
            return False
        literals = catalog_ast_literal_text(node)
        if any(Path(literal).name in GATEWAY_RESOURCE_NAMES for literal in literals):
            return True
        if isinstance(node, ast.Name):
            return catalog_ast_binding(node) in tainted_bindings
        if isinstance(node, ast.Attribute):
            return node.attr in GATEWAY_OLD_HANDLES or catalog_ast_has_resource(node.value)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            if catalog_ast_binding(node.func) in tainted_bindings:
                return True
        return any(catalog_ast_has_resource(child) for child in ast.iter_child_nodes(node))

    for node in nodes:
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else (node.target,)
            for target in targets:
                if isinstance(target, ast.Name) and node.value is not None:
                    literal_bindings.setdefault(catalog_ast_binding(target), set()).add(node.value)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            parameters = (*node.args.posonlyargs, *node.args.args)
            for parameter, default in zip(reversed(parameters), reversed(node.args.defaults)):
                literal_bindings.setdefault(resolve_binding(parameter.arg, node), set()).add(default)
            for parameter, default in zip(node.args.kwonlyargs, node.args.kw_defaults):
                if default is not None:
                    literal_bindings.setdefault(resolve_binding(parameter.arg, node), set()).add(default)
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                imported_name = alias.asname or alias.name.split(".")[0]
                binding = resolve_binding(imported_name, node_scopes[node])
                call_aliases[binding] = alias.name
                if alias.name in GATEWAY_OLD_HANDLES:
                    tainted_bindings.add(binding)
    changed = True
    while changed:
        previous = (tainted_bindings.copy(), call_aliases.copy(),
                    {key: values.copy() for key, values in literal_bindings.items()}, functions.copy())
        for node in nodes:
            if isinstance(node, (ast.Assign, ast.AnnAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else (node.target,)
                for target in targets:
                    for child in ast.walk(target):
                        if not isinstance(child, ast.Name):
                            continue
                        binding = catalog_ast_binding(child)
                        if catalog_ast_has_resource(node.value):
                            tainted_bindings.add(binding)
                        dotted = catalog_ast_dotted_name(node.value)
                        references_target = any(
                            isinstance(reference, ast.Name) and catalog_ast_binding(reference) == binding
                            for reference in ast.walk(node.value)) if node.value is not None else False
                        if dotted and not references_target:
                            # Sink identity is the method token. Dropping receiver
                            # spelling also keeps cyclic aliases a finite lattice.
                            call_aliases[binding] = dotted.rsplit(".", 1)[-1]
                        if isinstance(node.value, ast.Name):
                            function = functions.get(catalog_ast_binding(node.value))
                            if function is not None:
                                functions[binding] = function
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                function = functions.get(catalog_ast_binding(node.func))
                if function is not None:
                    parameters = (*function.args.posonlyargs, *function.args.args)
                    for parameter, argument in zip(parameters, node.args):
                        if catalog_ast_has_resource(argument):
                            tainted_bindings.add(resolve_binding(parameter.arg, function))
                        literal_bindings.setdefault(resolve_binding(parameter.arg, function), set()).add(argument)
                    for keyword in node.keywords:
                        if keyword.arg and catalog_ast_has_resource(keyword.value):
                            tainted_bindings.add(resolve_binding(keyword.arg, function))
                        if keyword.arg:
                            literal_bindings.setdefault(resolve_binding(keyword.arg, function), set()).add(keyword.value)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if any(isinstance(child, ast.Return) and node_scopes[child] is node
                       and catalog_ast_has_resource(child.value)
                       for child in ast.walk(node)):
                    tainted_bindings.add(resolve_binding(node.name, node_scopes[node]))
        changed = previous != (tainted_bindings, call_aliases, literal_bindings, functions)
    if permitted:
        return []
    return [(node.lineno, ast.unparse(node)) for node in nodes if isinstance(node, ast.Call)
            and catalog_ast_dotted_name(node.func).rsplit(".", 1)[-1] in GATEWAY_ACCESS_METHODS
            and catalog_ast_has_resource(node)]


def catalog_ast_import_violations(source):
    """Reject adapter imports and private gateway symbols, including aliased ports."""
    tree = ast.parse(source)
    node_scopes, resolve_binding = catalog_ast_lexical_bindings(tree)
    aliases = {}
    string_values = {}
    violations = []

    def catalog_ast_import_symbol(node):
        if isinstance(node, ast.Name):
            return aliases.get(resolve_binding(node.id, node_scopes[node]), node.id)
        if isinstance(node, ast.Attribute):
            return catalog_ast_import_symbol(node.value) + "." + node.attr
        return ""

    def catalog_ast_import_literal(node, active_bindings=frozenset()):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return frozenset((node.value,))
        if isinstance(node, ast.Name):
            binding = resolve_binding(node.id, node_scopes[node])
            if binding in active_bindings:
                return frozenset()
            return frozenset(value for expression in string_values.get(binding, ())
                             for value in catalog_ast_import_literal(expression, active_bindings | {binding}))
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            return frozenset(left + right for left, right in itertools.product(
                catalog_ast_import_literal(node.left, active_bindings),
                catalog_ast_import_literal(node.right, active_bindings)))
        if isinstance(node, ast.JoinedStr):
            return frozenset("".join(parts) for parts in itertools.product(*(
                catalog_ast_import_literal(part, active_bindings) for part in node.values)))
        if isinstance(node, ast.FormattedValue):
            return catalog_ast_import_literal(node.value, active_bindings)
        return frozenset()

    for node in ast.walk(tree):
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else (node.target,)
            for target in targets:
                if isinstance(target, ast.Name) and node.value is not None:
                    string_values.setdefault(resolve_binding(target.id, node_scopes[target]), set()).add(node.value)
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            module = node.module or "" if isinstance(node, ast.ImportFrom) else ""
            for alias in node.names:
                imported = module + "." + alias.name if module else alias.name
                if "catalog_store" in imported.split("."):
                    violations.append((node.lineno, ast.unparse(node)))
                local_name = alias.asname or alias.name.split(".")[0]
                aliases[resolve_binding(local_name, node_scopes[node])] = (
                    imported if isinstance(node, ast.ImportFrom) or alias.asname else local_name)
                if module.split(".")[-1] == "model_catalog" \
                        and (alias.name.startswith("_") or alias.name in GATEWAY_OLD_HANDLES):
                    violations.append((node.lineno, ast.unparse(node)))
    changed = True
    while changed:
        previous = aliases.copy()
        for node in ast.walk(tree):
            if isinstance(node, (ast.Assign, ast.AnnAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else (node.target,)
                for target in targets:
                    if not isinstance(target, ast.Name):
                        continue
                    binding = resolve_binding(target.id, node_scopes[target])
                    if node.value is not None and any(
                            isinstance(child, ast.Name)
                            and resolve_binding(child.id, node_scopes[child]) == binding
                            for child in ast.walk(node.value)):
                        continue
                    symbol = catalog_ast_import_symbol(node.value)
                    if symbol and symbol.rsplit(".", 1)[-1] in (
                            "model_catalog", "catalog_store", "import_module", "__import__"):
                        aliases[binding] = symbol.rsplit(".", 1)[-1]
        changed = previous != aliases
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute):
            parts = catalog_ast_import_symbol(node).split(".")
            if "model_catalog" in parts:
                port_index = parts.index("model_catalog")
                if any(part.startswith("_") or part in GATEWAY_OLD_HANDLES for part in parts[port_index + 1:]):
                    violations.append((node.lineno, ast.unparse(node)))
        if isinstance(node, ast.Call) and node.args:
            symbol = catalog_ast_import_symbol(node.func)
            values = catalog_ast_import_literal(node.args[0])
            if symbol.rsplit(".", 1)[-1] in ("import_module", "__import__") \
                    and any("catalog_store" in value.split(".") for value in values):
                violations.append((node.lineno, ast.unparse(node)))
            if symbol == "getattr" and len(node.args) > 1:
                target = catalog_ast_import_symbol(node.args[0]).split(".")
                members = catalog_ast_import_literal(node.args[1])
                if "model_catalog" in target \
                        and any(member.startswith("_") or member in GATEWAY_OLD_HANDLES for member in members):
                    violations.append((node.lineno, ast.unparse(node)))
    return violations


def test_catalog_gateway_physical_access_absence():
    paths = sorted(GATEWAY_SOURCE_ROOT.rglob("*.py"))
    violations = {str(path.relative_to(GATEWAY_REPO_ROOT)): findings for path in paths
                  if (findings := catalog_ast_access_violations(
                      path.read_text(), permitted=path.resolve() == GATEWAY_MODULE_PATH))}
    print(f"catalog physical-access denominator={len(paths)} Python modules; violations={violations}")
    assert not violations, violations


def test_catalog_gateway_public_boundary():
    assert importlib.util.find_spec("spendguard.store.reference_data.catalog_store") is None
    assert not (GATEWAY_OLD_HANDLES & set(vars(model_catalog)))
    violations = {str(path.relative_to(GATEWAY_REPO_ROOT)): findings
                  for path in sorted(GATEWAY_SOURCE_ROOT.rglob("*.py"))
                  if path.resolve() != GATEWAY_MODULE_PATH
                  and (findings := catalog_ast_import_violations(path.read_text()))}
    assert not violations, violations


@pytest.mark.parametrize("source", (
    'from pathlib import Path as P; P("catalog.sqlite").read_bytes()',
    'from model_catalog import DATA_PATH as p; open(p)',
    'from model_catalog import _SEED_PATH as p; p.read_text()',
    'p = "model_catalog" + ".json"; q = Path(p); q.write_bytes(data)',
    'def path():\n return Path("catalog_seed.sql")\npath().read_text()',
    'def load(p):\n return p.read_bytes()\nload(Path("catalog.sqlite"))',
    'from builtins import open as read_file; read_file("catalog_seed.sql")',
    'p = Path("catalog.sqlite"); sqlite3.connect(p)',
    'stem = "catalog"; p = Path(f"{stem}.sqlite"); p.read_bytes()',
    'def load_default(p=Path("catalog.sqlite")):\n return p.read_bytes()\nload_default()',
    'p = Path("catalog").with_suffix(".sqlite"); p.read_bytes()',
    'p = Path("catalog.sqlite"); reader = p.read_bytes; reader()',
    'def wrapper():\n def local_path():\n  return Path("catalog_seed.sql")\n return local_path()\nwrapper().read_bytes()',
    'import importlib as il; il.import_module("spendguard.store.reference_data." + "catalog_store")',
    'import spendguard as sg; alias = sg.model_catalog; alias._SEED_PATH.read_bytes()',
))
def test_catalog_gateway_ast_mutations_are_caught(source):
    assert catalog_ast_access_violations(source) or catalog_ast_import_violations(source)
    assert not catalog_ast_access_violations('print("author via model_catalog.json")')


@pytest.mark.parametrize("source", (
    'def catalog_name():\n p = "catalog.sqlite"\n return p\ndef ordinary_reader(p):\n return p.read_bytes()',
    'def catalog_reader(p):\n return p.read_bytes()\ndef ordinary_holder():\n p = "catalog.sqlite"\n return p',
    'def first():\n def helper():\n  return "catalog.sqlite"\n return helper()\ndef second():\n def helper():\n  return "other.sqlite"\n return Path(helper()).read_bytes()',
    'import model_catalog as port\ndef unrelated(port):\n return port._ordinary_member',
    'print("catalog.sqlite"); logger.info("model_catalog.json")',
))
def test_catalog_gateway_ast_scopes_remain_independent(source):
    assert not catalog_ast_access_violations(source)
    assert not catalog_ast_import_violations(source)


def test_catalog_gateway_shared_renderer_matches_generator(tmp_path):
    from spendguard.store.reference_data.catalog_provenance import PROVENANCE_AUTHORED, PROVENANCE_SOURCED
    from spendguard.store.reference_data.catalog_seed_rendering import render_catalog_seed

    generator_path = GATEWAY_REPO_ROOT / "scripts" / "catalog" / "gen_catalog_seed.py"
    specification = importlib.util.spec_from_file_location("catalog_gateway_generator_proof", generator_path)
    generator = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(generator)
    output = tmp_path / generator.DEFAULT_SEED_OUTPUT.name
    count = generator.generate_seed(generator.DEFAULT_CATALOG_SOURCE, generator.DEFAULT_SCHEMA_SOURCE, output)
    document = model_catalog.catalog_seed_document()
    rendered = render_catalog_seed(
        document, generator.DEFAULT_SCHEMA_SOURCE.read_text(),
        provenance_authored=PROVENANCE_AUTHORED, provenance_sourced=PROVENANCE_SOURCED)
    assert count == len(document["models"])
    assert output.read_text() == rendered


def test_catalog_gateway_price_intervals_round_trip_through_sqlite():
    document = copy.deepcopy(model_catalog.catalog_seed_document())
    model_id, record = next((item for item in sorted(document["models"].items())
                             if item[1].get("price")))
    interval = record.pop("price")
    interval.update(valid_from=GATEWAY_INTERVAL_VALID_FROM,
                    valid_until=GATEWAY_INTERVAL_VALID_UNTIL)
    record["price_intervals"] = [interval]

    _, _, database = model_catalog._prepare_catalog_bundle(document)
    reconstructed = model_catalog._catalog_document_from_sqlite_bytes(database)

    assert reconstructed == document
    assert reconstructed["models"][model_id]["price_intervals"] == [interval]


def test_catalog_model_behavior_is_the_detached_behavior_read_facade():
    model_id, record = next((item for item in sorted(model_catalog.all_records().items())
                             if item[1].get("capabilities") and item[1].get("reasoning")))
    expected = {field: copy.deepcopy(record[field]) for field in GATEWAY_BEHAVIOR_FIELDS if field in record}

    behavior = model_catalog.catalog_model_behavior(model_id)

    assert behavior == expected
    assert model_catalog.catalog_model_behavior(GATEWAY_UNKNOWN_MODEL_ID) is None
    behavior["capabilities"]["mode"] = GATEWAY_MUTATED_CAPABILITY_MODE
    assert model_catalog.catalog_model_behavior(model_id) == expected


def test_catalog_gateway_seed_parity():
    report = model_catalog.catalog_seed_parity()
    assert report["model_count"] == len(model_catalog.catalog_seed_document()["models"])
    assert report["model_count"] > 0
    assert report["differing_cells"] == [], report
    assert model_catalog.catalog_seed_document() == model_catalog.catalog_floor_document()
    print(f"catalog parity {report['model_count']}/{report['model_count']}; differing cells=0")


def test_catalog_gateway_authoring_and_seed_regeneration(tmp_path):
    with model_catalog.catalog_authoring_workspace(tmp_path):
        before = model_catalog.catalog_seed_document()
        model_id = next(iter(before["models"]))
        changed = copy.deepcopy(before)
        changed["models"][model_id]["vision"] = not bool(before["models"][model_id].get("vision"))
        model_catalog.author_catalog_records(lambda document, snapshot: changed, reason=GATEWAY_TEST_REASON, layer="seed")
        assert model_catalog.catalog_seed_document() == changed
        assert model_catalog.catalog_floor_document() == changed
        assert not model_catalog.catalog_seed_parity()["differing_cells"]


def test_catalog_gateway_authoring_failure_is_atomic(tmp_path, monkeypatch):
    with model_catalog.catalog_authoring_workspace(tmp_path):
        before = model_catalog.catalog_seed_document()

        def fail_catalog_seed_build(*args, **kwargs):
            raise OSError(GATEWAY_TEST_REASON)

        monkeypatch.setattr(model_catalog, "_prepare_catalog_bundle", fail_catalog_seed_build)
        with pytest.raises(OSError, match=GATEWAY_TEST_REASON):
            model_catalog.author_catalog_records(lambda document, snapshot: document, reason=GATEWAY_TEST_REASON, layer="seed")
        assert model_catalog.catalog_seed_document() == before
        assert not model_catalog.catalog_seed_parity()["differing_cells"]


def test_catalog_gateway_both_writers_use_authoring_api(tmp_path, monkeypatch):
    with model_catalog.catalog_authoring_workspace(tmp_path):
        calls = []
        author = model_catalog.author_catalog_records

        def record_catalog_author_call(*args, **kwargs):
            calls.append(kwargs.get("layer", "local"))
            return author(*args, **kwargs)

        monkeypatch.setattr(model_catalog, "author_catalog_records", record_catalog_author_call)
        model = next(record for record in model_catalog.all_records().values() if record.get("price"))
        price = model["price"]
        catalog_price_authoring.set_catalog_model_price(
            model["id"], input=price["in_"], output=price["out"],
            source=GATEWAY_PRICE_SOURCE, verified=price["verified"])
        assert calls == ["local"]
        monkeypatch.setattr(sync_capabilities, "_updates_for", lambda records: (
            {model["id"]: {"vision": not bool(model.get("vision"))}}, {}))
        sync_capabilities.sync_capabilities()
        assert calls == ["local", "seed"]
        assert not model_catalog.catalog_seed_parity()["differing_cells"]


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "--noconftest", "-o", "addopts=", "-q", "--tb=short"]))
