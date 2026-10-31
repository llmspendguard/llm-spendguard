"""Mechanical snapshot provenance and finite-contract guard, with mutation controls.

The inventory is the grounded set of monetary read roots, not a keyword
classifier. Newly decorated roots join the denominator automatically; a newly
reachable unwrapped ledger aggregate is reported as UNREVIEWED.
"""
import ast
from pathlib import Path

PROJECTION_GUARD_PACKAGE = Path(__file__).resolve().parents[1] / "src" / "spendguard"
PROJECTION_GUARD_READ_ROOTS = {
    "budget": {"spent_since", "spent_by_job", "spent_all_time", "meta_spent_since", "external_spent_since"},
    "calls": {"intent_spend"},
    "gate": set(),
    "receipt": set(),
}
PROJECTION_GUARD_CONSUMERS = {
    "budget": {"exceeded", "meta_exceeded", "external_exceeded"},
    "gate": {"_budget_check", "_meta_gate", "gate_external", "_intent_door_gate",
             "_rt_precheck_usd", "_decide_and_account"},
    "calls": {"_flow_start_usd"},
    "receipt": {"emit_flow"},
}
PROJECTION_GUARD_FINITE_ROOTS = {"exceeded", "meta_exceeded", "external_exceeded"}
PROJECTION_GUARD_SNAPSHOT_DECORATOR = "budget_projection_snapshot"
PROJECTION_GUARD_FINITE_DECORATOR = "finite_budget_projection"
PROJECTION_GUARD_WRITER_FACTORIES = {
    "_calls_db", "_ledger_db", "current_ledger_writer_connection", "pooled_ledger_conn",
    "connect", "SpendLedger",
}
PROJECTION_GUARD_AGGREGATES = {"spent_dec", "running_llm_total_dec", "meta_dec", "sum_by",
                               "_bounded_category_total"}
PROJECTION_GUARD_OWNED_MARKER = "_budget_projection_connection_owned"
PROJECTION_GUARD_FACADE = "_ledger"
# Flow receipt est-value reporting is a different axis, not a monetary cap or
# billed-spend baseline. Its existing read-only implementation remains intact.
PROJECTION_GUARD_DISPLAY_BOUNDARIES = {("receipt", "_receipt_est_records")}


def projection_guard_call_identity(expression):
    if isinstance(expression, ast.Name):
        return expression.id
    if isinstance(expression, ast.Attribute):
        prefix = projection_guard_call_identity(expression.value)
        return f"{prefix}.{expression.attr}" if prefix else expression.attr
    if (isinstance(expression, ast.Call) and isinstance(expression.func, ast.Name)
            and expression.func.id == "getattr" and len(expression.args) >= 2
            and isinstance(expression.args[1], ast.Constant)
            and isinstance(expression.args[1].value, str)):
        return expression.args[1].value
    return ""


def inspect_budget_projection_seam_sources(sources):
    failures = []
    unreviewed = []
    checked = []
    for module, source in sources.items():
        tree = ast.parse(source)
        functions = {node.name: node for node in tree.body
                     if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}
        expected = PROJECTION_GUARD_READ_ROOTS[module]
        import_aliases = {alias.asname or alias.name: alias.name
                          for item in ast.walk(tree) if isinstance(item, (ast.Import, ast.ImportFrom))
                          for alias in item.names}
        discovered = {name for name, node in functions.items()
                      if any(projection_guard_call_identity(decorator).split(".")[-1]
                             == PROJECTION_GUARD_SNAPSHOT_DECORATOR for decorator in node.decorator_list)}
        for name in sorted(expected | discovered):
            node = functions.get(name)
            if node is None:
                unreviewed.append(f"Missing grounded root {module}.{name}")
                continue
            checked.append(f"{module}.{name}")
            decorators = {projection_guard_call_identity(item).split(".")[-1]
                          for item in node.decorator_list}
            if PROJECTION_GUARD_SNAPSHOT_DECORATOR not in decorators:
                failures.append(f"{module}.{name}: snapshot seam absent")
            for call in (item for item in ast.walk(node) if isinstance(item, ast.Call)):
                identity = projection_guard_call_identity(call.func)
                bare_identity = import_aliases.get(identity, identity).split(".")[-1]
                if bare_identity in PROJECTION_GUARD_WRITER_FACTORIES:
                    failures.append(f"{module}.{name}:{call.lineno}: raw writer {identity}")
        if module == "budget":
            for name in sorted(PROJECTION_GUARD_FINITE_ROOTS):
                node = functions.get(name)
                if node is None:
                    unreviewed.append(f"Missing projection {module}.{name}")
                    continue
                decorators = {projection_guard_call_identity(item).split(".")[-1]
                              for item in node.decorator_list}
                if PROJECTION_GUARD_FINITE_DECORATOR not in decorators:
                    failures.append(f"{module}.{name}: finite contract absent")
                # A projection cannot introduce a direct aggregate beside the seam.
                for call in (item for item in ast.walk(node) if isinstance(item, ast.Call)):
                    if isinstance(call.func, ast.Attribute) and call.func.attr in PROJECTION_GUARD_AGGREGATES:
                        unreviewed.append(f"{module}.{name}:{call.lineno}: direct aggregate")
        # Follow every local helper call from the projection consumers. This
        # catches a new window/helper even when its name contains no budget
        # terminology. The facade itself is the context-selected interface.
        pending = [(name, False) for name in PROJECTION_GUARD_CONSUMERS[module]]
        # Gate functions are enforcement entrypoints: an added direct SQL or
        # aggregate read anywhere in that module must be reviewed, even if its
        # author has not wired the helper into an existing consumer yet.
        if module == "gate":
            pending.extend((name, False) for name in functions)
        visited = set()
        while pending:
            name, snapshot_owned = pending.pop()
            if (name, snapshot_owned) in visited:
                continue
            if name not in functions:
                unreviewed.append(f"Missing consumer {module}.{name}")
                continue
            visited.add((name, snapshot_owned))
            node = functions[name]
            snapshot_owned = snapshot_owned or any(
                projection_guard_call_identity(item).split(".")[-1]
                == PROJECTION_GUARD_SNAPSHOT_DECORATOR for item in node.decorator_list)
            for call in (item for item in ast.walk(node) if isinstance(item, ast.Call)):
                identity = projection_guard_call_identity(call.func)
                bare_identity = import_aliases.get(identity, identity).split(".")[-1]
                if bare_identity in PROJECTION_GUARD_AGGREGATES or bare_identity == "execute":
                    if not snapshot_owned:
                        unreviewed.append(f"{module}.{name}:{call.lineno}: aggregate outside snapshot")
                if bare_identity in PROJECTION_GUARD_WRITER_FACTORIES:
                    failures.append(f"{module}.{name}:{call.lineno}: reachable raw writer {identity}")
                if (identity in functions and identity != PROJECTION_GUARD_FACADE
                        and (module, identity) not in PROJECTION_GUARD_DISPLAY_BOUNDARIES):
                    pending.append((identity, snapshot_owned))
    return checked, failures, unreviewed


def inspect_budget_projection_category_ownership(ledger_source, budget_source):
    """Check the grounded downstream category branch actually uses its owner.

    This checks two exact call contracts, not arbitrary Python provenance.
    Runtime snapshot-identity and pending-transaction tests cover their behavior.
    """
    failures = []
    ledger_tree = ast.parse(ledger_source)
    category_reader = next(node for node in ast.walk(ledger_tree)
                           if isinstance(node, ast.FunctionDef) and node.name == "_bounded_category_total")
    owned_branches = [node for node in ast.walk(category_reader) if isinstance(node, ast.If)
                      and any(isinstance(item, ast.Constant) and item.value == PROJECTION_GUARD_OWNED_MARKER
                              for item in ast.walk(node.test))]
    if not any(any(isinstance(item, ast.Call) and isinstance(item.func, ast.Attribute)
                   and item.func.attr == "_cat_dec" for statement in branch.body for item in ast.walk(statement))
               for branch in owned_branches):
        failures.append("ledger._bounded_category_total: owned category connection branch absent")
    external = next(node for node in ast.parse(budget_source).body
                    if isinstance(node, ast.FunctionDef) and node.name == "external_spent_since")
    if not any(isinstance(item, ast.Call) and isinstance(item.func, ast.Attribute)
               and item.func.attr == "_bounded_category_total" for item in ast.walk(external)):
        failures.append("budget.external_spent_since: owned category aggregate absent")
    return failures


def run_budget_projection_seam_source_guard():
    sources = {module: (PROJECTION_GUARD_PACKAGE / f"{module}.py").read_text()
               for module in PROJECTION_GUARD_READ_ROOTS}
    checked, failures, unreviewed = inspect_budget_projection_seam_sources(sources)
    ledger_source = (PROJECTION_GUARD_PACKAGE / "ledger.py").read_text()
    failures.extend(inspect_budget_projection_category_ownership(ledger_source, sources["budget"]))
    total = sum(len(roots) for roots in PROJECTION_GUARD_READ_ROOTS.values())
    total += sum(1 for root in checked if root.split(".")[-1]
                 not in PROJECTION_GUARD_READ_ROOTS[root.split(".")[0]])
    print(f"Budget projection read coverage {len(checked)}/{total}; "
          f"consumer modules {len(sources)}/{len(PROJECTION_GUARD_CONSUMERS)}; "
          f"FAIL {len(failures)}; UNREVIEWED {len(unreviewed)}")
    assert not failures and not unreviewed, "\n".join(failures + unreviewed)
    category_mutation = ledger_source.replace(PROJECTION_GUARD_OWNED_MARKER,
                                             "deliberately_removed_projection_owner")
    assert inspect_budget_projection_category_ownership(category_mutation, sources["budget"]), (
        "removing downstream snapshot ownership must fail")
    mutation = ast.parse(sources["calls"])
    intent = next(node for node in mutation.body if isinstance(node, ast.FunctionDef)
                  and node.name == "intent_spend")
    intent.body.insert(0, ast.Expr(value=ast.Call(func=ast.Name(id="_calls_db", ctx=ast.Load()),
                                               args=[], keywords=[])))
    mutated = dict(sources, calls=ast.unparse(ast.fix_missing_locations(mutation)))
    _, rejected, _ = inspect_budget_projection_seam_sources(mutated)
    assert any("raw writer _calls_db" in failure for failure in rejected), rejected
    intent.decorator_list = []
    mutated["calls"] = ast.unparse(ast.fix_missing_locations(mutation))
    _, rejected, _ = inspect_budget_projection_seam_sources(mutated)
    assert any("snapshot seam absent" in failure for failure in rejected), rejected
    budget_mutation = ast.parse(sources["budget"])
    projection = next(node for node in budget_mutation.body if isinstance(node, ast.FunctionDef)
                      and node.name == "exceeded")
    helper_name = "deliberate_projection_guard_unreviewed_window"
    helper = ast.parse(f"def {helper_name}():\n    return _ledger().spent_dec()\n").body[0]
    budget_mutation.body.append(helper)
    projection.body.insert(0, ast.Expr(value=ast.Call(func=ast.Name(id=helper_name, ctx=ast.Load()),
                                                   args=[], keywords=[])))
    helper_sources = dict(sources, budget=ast.unparse(ast.fix_missing_locations(budget_mutation)))
    _, _, unreviewed_helper = inspect_budget_projection_seam_sources(helper_sources)
    assert any(helper_name in gap for gap in unreviewed_helper), unreviewed_helper
    projection.body.insert(0, ast.parse("_ledger().spent_dec()").body[0])
    helper_sources["budget"] = ast.unparse(ast.fix_missing_locations(budget_mutation))
    _, _, direct_read = inspect_budget_projection_seam_sources(helper_sources)
    assert any("exceeded" in gap and "direct aggregate" in gap for gap in direct_read), direct_read
    projection.decorator_list = []
    helper_sources["budget"] = ast.unparse(ast.fix_missing_locations(budget_mutation))
    _, finite_failures, _ = inspect_budget_projection_seam_sources(helper_sources)
    assert any("finite contract absent" in failure for failure in finite_failures), finite_failures
    print("Downstream ownership contracts 2/2; negative controls rejected raw writer, direct aggregate, "
          "new helper, missing snapshot/finite contracts and removed category ownership")


if __name__ == "__main__":
    run_budget_projection_seam_source_guard()
