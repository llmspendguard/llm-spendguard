"""Guard the shared CLI resolver bindings and pin precedence for all CLI lanes."""

import ast
import importlib
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


CLI_BIN_CASES = (
    ("subscription_exec", "claude", "SPENDGUARD_CLAUDE_BIN"),
    ("codex_exec", "codex", "SPENDGUARD_CODEX_BIN"),
    ("antigravity_exec", "agy", "SPENDGUARD_AGY_BIN"),
    ("kimi_exec", "kimi", "SPENDGUARD_KIMI_BIN"),
    ("grok_exec", "grok", "SPENDGUARD_GROK_BIN"),
    ("zcode_exec", "zcode", "SPENDGUARD_ZCODE_BIN"),
)
EXECUTOR_PACKAGE = "spendguard.generate.execution"
RESOLVER_QUALNAME_SUFFIX = "cli_resolver.<locals>.resolve_bound_cli"
EXECUTABLE_MODE = 0o755


class CliBinResolverMergeTests(unittest.TestCase):
    def test_structure(self):
        for module_name, _, _ in CLI_BIN_CASES:
            with self.subTest(module=module_name):
                module = importlib.import_module(f"{EXECUTOR_PACKAGE}.{module_name}")
                tree = ast.parse(Path(module.__file__).read_text())
                self.assertFalse(any(isinstance(node, ast.FunctionDef) and node.name == "_bin"
                                     for node in ast.walk(tree)))
                self.assertTrue(callable(module._bin))
                self.assertTrue(module._bin.__qualname__.endswith(RESOLVER_QUALNAME_SUFFIX))

    def test_pin_wins_and_missing_pin_fails_loud(self):
        with tempfile.TemporaryDirectory() as directory:
            for module_name, cli_name, env_var in CLI_BIN_CASES:
                with self.subTest(module=module_name):
                    module = importlib.import_module(f"{EXECUTOR_PACKAGE}.{module_name}")
                    pinned = Path(directory) / f"{cli_name}-pinned"
                    stray = Path(directory) / cli_name
                    missing = Path(directory) / f"{cli_name}-missing"
                    pinned.write_text("#!/bin/sh\n")
                    pinned.chmod(EXECUTABLE_MODE)
                    stray.write_text("#!/bin/sh\n")
                    stray.chmod(EXECUTABLE_MODE)
                    with patch("shutil.which", return_value=str(stray)) as which:
                        with patch.dict(os.environ, {env_var: str(pinned)}):
                            self.assertEqual(module._bin(), str(pinned))
                        with patch.dict(os.environ, {env_var: str(missing)}):
                            self.assertIsNone(module._bin())
                        which.assert_not_called()


if __name__ == "__main__":
    unittest.main()
