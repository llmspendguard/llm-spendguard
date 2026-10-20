"""Structural guard for the enumerated served ledger connection and scope graph."""
import ast
import pathlib
import unittest

REQUEST_LEDGER_SOURCE_ROOT = pathlib.Path(__file__).resolve().parents[1] / "src" / "spendguard"
REQUEST_LEDGER_READER_MODULES = (
    "mcp_server", "receipt", "budget", "ledger", "guard", "lane_quota_cost",
    "lane_bandit", "intent_model_prior", "provider_tokens", "reliability", "bulkgate",
    "callio", "learn", "models", "best_value", "calibrate", "semcache", "batch_tracker",
)
REQUEST_LEDGER_OFFLINE_CONNECTION_FUNCTIONS = {("budget", "snapshot")}
REQUEST_LEDGER_REQUIRED_EDGES = {
    "ledger_read": {
        "_ledger_bounded_read_handle": ("open_ledger_connection", "set_progress_handler", "rollback", "close"),
        "ledger_request_scope": ("cancel_ledger_request",),
    },
    "config": {
        "open_ledger_connection": ("connect",),
        "_dispatch_home_aligned_cursor_operation": ("dispatch_managed_request_cursor_step",),
        "_dispatch_home_aligned_cursor_shortcut": ("_dispatch_home_aligned_cursor_operation",),
    },
    "ledger_request_cursor": {
        "dispatch_managed_request_cursor_step": (
            "current_ledger_request_scope", "set_authorizer", "set_progress_handler",
            "register_request_read_connection", "register_request_read_cursor",
            "dispose_managed_request_cursor"),
    },
    "mcp_server": {
        "handle": ("ledger_request_scope",),
        "_mcp_http_request_ledger_lifetime": ("ledger_request_scope", "cancel_ledger_request"),
    },
    "codex_daemon_broker": {
        "_serve_one": ("_broker_request_ledger_lifetime",),
        "_broker_request_ledger_lifetime": ("ledger_request_scope", "cancel_ledger_request"),
    },
    "codex_daemon": {"pool_run_warm": ("ledger_request_scope", "copy_context", "cancel_ledger_request")},
}


def _request_ledger_ast_callee(node):
    return node.attr if isinstance(node, ast.Attribute) else node.id if isinstance(node, ast.Name) else None


def _assert_request_ledger_connection_graph(trees):
    """Check actual AST call edges and managed factory, never source substrings."""
    for module in REQUEST_LEDGER_READER_MODULES:
        tree = trees[module]
        sqlite_modules = {alias.asname or alias.name for node in ast.walk(tree)
                          if isinstance(node, ast.Import) for alias in node.names if alias.name == "sqlite3"}
        sqlite_connects = {alias.asname or alias.name for node in ast.walk(tree)
                           if isinstance(node, ast.ImportFrom) and node.module == "sqlite3"
                           for alias in node.names if alias.name == "connect"}
        excluded = {id(child) for definition in ast.walk(tree)
                    if isinstance(definition, ast.FunctionDef)
                    and (module, definition.name) in REQUEST_LEDGER_OFFLINE_CONNECTION_FUNCTIONS
                    for child in ast.walk(definition)}
        for node in ast.walk(tree):
            if id(node) in excluded:
                continue
            if not isinstance(node, ast.Call):
                continue
            direct_connect = isinstance(node.func, ast.Name) and node.func.id in sqlite_connects
            module_connect = (isinstance(node.func, ast.Attribute) and node.func.attr == "connect"
                              and isinstance(node.func.value, ast.Name) and node.func.value.id in sqlite_modules)
            assert not (direct_connect or module_connect), (module, node.lineno, "unmanaged request ledger read")
    for module, required_functions in REQUEST_LEDGER_REQUIRED_EDGES.items():
        definitions = {node.name: node for node in ast.walk(trees[module])
                       if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}
        for function, required_callees in required_functions.items():
            calls = {_request_ledger_ast_callee(node.func) for node in ast.walk(definitions[function])
                     if isinstance(node, ast.Call)}
            assert set(required_callees) <= calls, (module, function, set(required_callees) - calls)
    config_open = next(node for node in ast.walk(trees["config"])
                       if isinstance(node, ast.FunctionDef) and node.name == "open_ledger_connection")
    connections = [node for node in ast.walk(config_open) if isinstance(node, ast.Call)
                   and _request_ledger_ast_callee(node.func) == "connect"]
    assert connections
    for call in connections:
        assert any(keyword.arg == "factory" and isinstance(keyword.value, ast.Name)
                   and keyword.value.id == "_CountedLedgerConnection" for keyword in call.keywords), "unguarded factory"


class RequestLedgerReadBoundsGraphGuards(unittest.TestCase):
    def test_request_ledger_read_graph_and_unbounded_mutation(self):
        modules = set(REQUEST_LEDGER_READER_MODULES) | set(REQUEST_LEDGER_REQUIRED_EDGES)
        trees = {module: ast.parse((REQUEST_LEDGER_SOURCE_ROOT / (module + ".py")).read_text())
                 for module in modules}
        _assert_request_ledger_connection_graph(trees)
        # Reintroduce the exact historical bypass as an AST mutation in a served
        # reader; the same production-graph assertion must reject it.
        mutation = ast.parse("sqlite3.connect(ledger_path)").body[0]
        trees["receipt"].body.append(mutation)
        with self.assertRaises(AssertionError):
            _assert_request_ledger_connection_graph(trees)


if __name__ == "__main__":
    unittest.main()
