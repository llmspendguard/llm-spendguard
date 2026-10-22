"""Offline guards for the canonical warm default and conservative Codex execution cap."""
import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from spendguard import codex_daemon, config, config_schema, dispatch

# Track the one SSOT (config.CODEX_POOL_SIZE_DEFAULT), not a frozen literal: this guard checks that the
# resolution path returns the DECLARED default when nothing overrides — it does not re-pin the number, so an
# intentional default change (e.g. 12→20) never forces a hand-edit here while the override cases below still prove
# env/cfg win over whatever the default is.
SCHEDULER_EXPECTED_WARM_DEFAULT = config.CODEX_POOL_SIZE_DEFAULT
SCHEDULER_OVERRIDE_WARM_SIZE = config.CODEX_POOL_SIZE_DEFAULT - 1
SCHEDULER_CEILING_CONFIGURATIONS = ((2, 11, 12), (11, 3, 12), (11, 12, 4))
SCHEDULER_CODEX_VENDOR = "openai"
SCHEDULER_CODEX_LANE = "codex"


class SchedulerPhaseTwoCeilingsGuard(unittest.TestCase):
    def test_scheduler_canonical_warm_default_and_overrides(self):
        with patch.dict(os.environ, {}, clear=True), patch.object(config, "_cfg", return_value={}):
            self.assertEqual(config.codex_daemon_pool_settings()["size"], SCHEDULER_EXPECTED_WARM_DEFAULT)
            schema = next(setting for setting in config_schema.SETTINGS
                          if setting["key"] == "codex_daemon_pool_size")
            self.assertEqual(schema["default"], config.CODEX_POOL_SIZE_DEFAULT)
            with patch.dict(os.environ, {"SPENDGUARD_CODEX_DAEMON_POOL_SIZE": str(SCHEDULER_OVERRIDE_WARM_SIZE)}):
                self.assertEqual(config.codex_daemon_pool_settings()["size"], SCHEDULER_OVERRIDE_WARM_SIZE)
            with patch.object(config, "_cfg", return_value={"advisor": {
                    "codex_daemon_pool_size": SCHEDULER_OVERRIDE_WARM_SIZE}}):
                self.assertEqual(config.codex_daemon_pool_settings()["size"], SCHEDULER_OVERRIDE_WARM_SIZE)

    def test_scheduler_every_execution_consumer_honors_tightest_dimension(self):
        from spendguard import adapters
        for warm_size, global_cap, lane_cap in SCHEDULER_CEILING_CONFIGURATIONS:
            expected = min(warm_size, global_cap, lane_cap)
            settings = {"advisor": {"codex_daemon_pool_size": warm_size}, "dispatch": {
                "global_concurrency": global_cap, "lane_concurrency_codex": lane_cap}}
            with self.subTest(settings=settings), patch.dict(os.environ, {}, clear=True), \
                    patch.object(config, "_cfg", return_value=settings), \
                    patch.object(adapters, "_lane_for", return_value=(SCHEDULER_CODEX_LANE, None)):
                self.assertEqual(dispatch.effective_codex_concurrency(), expected)
                governor = type(dispatch._GOV)()
                self.assertEqual(governor._key_and_limit(SCHEDULER_CODEX_VENDOR, None)[1], expected)
                self.assertEqual(governor._bucket(SCHEDULER_CODEX_VENDOR, None).limit, expected)
                with patch.object(dispatch, "_GOV", governor):
                    self.assertEqual(dispatch.lane_free(SCHEDULER_CODEX_LANE), expected)
                pool = codex_daemon._CodexDaemonPool(SimpleNamespace)
                self.assertEqual(pool._size, expected)
                self.assertEqual(pool._slots.limit, expected)
                # The aggregate backstop still protects all lanes together at its configured bound.
                self.assertEqual(governor._global_sem()._initial_value, global_cap)


if __name__ == "__main__":
    unittest.main()
