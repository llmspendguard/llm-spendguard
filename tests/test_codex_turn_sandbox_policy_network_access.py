import os
import unittest
from unittest import mock

from spendguard.manage.token_operations.codex_daemon import codex_turn_sandbox_policy
from spendguard.store.configuration import config_schema, config_store


class CodexTurnSandboxPolicyNetworkAccessGuards(unittest.TestCase):
    def test_codex_turn_sandbox_policy_has_complete_default_shapes(self):
        with mock.patch.dict(os.environ, {}, clear=True), mock.patch.object(config_store, "_cfg", return_value={}):
            self.assertEqual(codex_turn_sandbox_policy("workspace-write"), {
                "type": "workspaceWrite", "networkAccess": True})
            self.assertEqual(codex_turn_sandbox_policy("read-only"), {
                "type": "readOnly", "networkAccess": False})

    def test_codex_turn_sandbox_policy_rejects_unrecognized_modes(self):
        for sandbox in ("danger-full-access", "unexpected-mode"):
            with self.subTest(sandbox=sandbox):
                with self.assertRaisesRegex(ValueError, sandbox):
                    codex_turn_sandbox_policy(sandbox)
                with self.assertRaisesRegex(ValueError, sandbox):
                    config_store.codex_network_access_enabled_for_sandbox(sandbox)

    def test_recognized_codex_sandbox_modes_have_consistent_complete_policies(self):
        expected_types = {
            config_store.CODEX_READ_ONLY_SANDBOX_MODE: "readOnly",
            config_store.CODEX_WORKSPACE_WRITE_SANDBOX_MODE: "workspaceWrite",
        }
        checked_modes = 0
        with mock.patch.dict(os.environ, {}, clear=True), mock.patch.object(config_store, "_cfg", return_value={}):
            for sandbox in config_store.CODEX_SANDBOX_MODES:
                policy = codex_turn_sandbox_policy(sandbox)
                self.assertEqual(policy["type"], expected_types[sandbox])
                self.assertIsInstance(policy["networkAccess"], bool)
                checked_modes += 1
        self.assertGreater(checked_modes, 0)

    def test_codex_turn_sandbox_policy_knobs_are_independent(self):
        overridden_config = {"advisor": {"codex_workspace_write_network_access": False}}
        with mock.patch.dict(os.environ, {}, clear=True), \
                mock.patch.object(config_store, "_cfg", return_value=overridden_config):
            self.assertEqual(codex_turn_sandbox_policy("workspace-write"), {
                "type": "workspaceWrite", "networkAccess": False})
            self.assertEqual(codex_turn_sandbox_policy("read-only"), {
                "type": "readOnly", "networkAccess": False})
        overridden_config = {"advisor": {"codex_read_only_network_access": True}}
        with mock.patch.dict(os.environ, {}, clear=True), \
                mock.patch.object(config_store, "_cfg", return_value=overridden_config):
            self.assertEqual(codex_turn_sandbox_policy("workspace-write"), {
                "type": "workspaceWrite", "networkAccess": True})
            self.assertEqual(codex_turn_sandbox_policy("read-only"), {
                "type": "readOnly", "networkAccess": True})

    def test_codex_turn_sandbox_policy_env_knobs_are_independent(self):
        with mock.patch.dict(os.environ, {
                "SPENDGUARD_CODEX_WORKSPACE_WRITE_NETWORK_ACCESS": "false",
        }, clear=True), mock.patch.object(config_store, "_cfg", return_value={}):
            self.assertEqual(codex_turn_sandbox_policy("workspace-write"), {
                "type": "workspaceWrite", "networkAccess": False})
            self.assertEqual(codex_turn_sandbox_policy("read-only"), {
                "type": "readOnly", "networkAccess": False})
        with mock.patch.dict(os.environ, {
                "SPENDGUARD_CODEX_READ_ONLY_NETWORK_ACCESS": "true",
        }, clear=True), mock.patch.object(config_store, "_cfg", return_value={}):
            self.assertEqual(codex_turn_sandbox_policy("workspace-write"), {
                "type": "workspaceWrite", "networkAccess": True})
            self.assertEqual(codex_turn_sandbox_policy("read-only"), {
                "type": "readOnly", "networkAccess": True})

    def test_codex_sandbox_network_access_settings_are_declared(self):
        expected = {
            "codex_workspace_write_network_access": (
                "SPENDGUARD_CODEX_WORKSPACE_WRITE_NETWORK_ACCESS", True),
            "codex_read_only_network_access": (
                "SPENDGUARD_CODEX_READ_ONLY_NETWORK_ACCESS", False),
        }
        declarations = {setting["key"]: setting for setting in config_schema.SETTINGS
                        if setting["section"] == "advisor" and setting["key"] in expected}
        self.assertEqual(set(declarations), set(expected))
        for key, (env, default) in expected.items():
            declaration = declarations[key]
            self.assertEqual(declaration["store"], f"config.json:advisor.{key}")
            self.assertEqual(declaration["env"], env)
            self.assertIs(declaration["default"], default)
            self.assertEqual(declaration["kind"], "bool")


if __name__ == "__main__":
    unittest.main()
