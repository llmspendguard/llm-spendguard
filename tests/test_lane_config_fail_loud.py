"""Unreadable routing configuration must never become a cached metered default."""
import json
import os
import sys
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from _pytest_fixture_bridge import align_test_startup_home

align_test_startup_home(prefix=__name__)


class LaneConfigurationFailureTests(unittest.TestCase):
    def test_lane_config_error_stops_dispatch_before_paid_key_lookup(self):
        config.CONFIG_JSON.write_text("{")
        config.cfg_invalidate()
        provider = next(iter(adapters._LANES))
        prompt = self.id()
        with patch.object(config, "api_key", side_effect=AssertionError("paid dispatch reached")) as paid_key:
            with self.assertRaises(config.ConfigurationReadError):
                adapters._call_once(f"{provider}:{prompt}", prompt, max_tokens=len(prompt))
            paid_key.assert_not_called()

    def test_lane_config_error_during_lane_import_propagates_with_env_override(self):
        provider = next(iter(adapters._LANES))
        with patch.dict(os.environ, {"SPENDGUARD_ADVISOR_EXECUTOR": "pool"}), patch.object(
            adapters, "_lane_cooling", return_value=False
        ), patch("importlib.import_module", side_effect=config.ConfigurationReadError(self.id())):
            with self.assertRaises(config.ConfigurationReadError):
                adapters._lane_for(provider)

    def test_lane_config_absence_and_empty_object_are_valid(self):
        config.cfg_invalidate()
        self.assertEqual(config._cfg(), {})
        config.CONFIG_JSON.write_text(json.dumps({}))
        self.assertEqual(config._cfg(), {})

    def test_lane_config_io_failures_retry_and_refuse_metered_executor(self):
        payload = {"advisor": {"executor": "pool"}}
        config.CONFIG_JSON.write_text(json.dumps(payload))
        for operation in ("stat", "read_text"):
            with self.subTest(operation=operation):
                config.cfg_invalidate()
                with patch.object(Path, operation, side_effect=PermissionError(operation)):
                    for consumer in (config._cfg, adapters._executor):
                        with self.assertRaises(RuntimeError) as refused:
                            consumer()
                        self.assertIn(str(config.CONFIG_JSON), str(refused.exception))
                        self.assertIn("metered fallback is refused", str(refused.exception))
                    self.assertIsNone(config._cfg._cache)
                # Same mtime: a failed attempt must not have memoized empty defaults.
                self.assertEqual(adapters._executor(), payload["advisor"]["executor"])

    def test_lane_config_invalid_json_and_schema_are_loud_and_recoverable(self):
        payload = {"advisor": {"executor": "pool"}}
        valid_text = json.dumps(payload)
        config.CONFIG_JSON.write_text(valid_text)
        for invalid_text in ("{", json.dumps([]), json.dumps(None)):
            with self.subTest(invalid_text=invalid_text):
                config.cfg_invalidate()
                with patch.object(Path, "read_text", return_value=invalid_text):
                    with self.assertRaises(RuntimeError):
                        adapters._executor()
                    self.assertIsNone(config._cfg._cache)
                self.assertEqual(config._cfg(), payload)

    def test_lane_config_changed_file_failure_discards_stale_cache(self):
        config.CONFIG_JSON.write_text(json.dumps({"advisor": {"executor": "pool"}}))
        config.cfg_invalidate()
        self.assertEqual(adapters._executor(), "pool")
        # Force the existing cache key to differ without relying on filesystem clock granularity.
        config._cfg._mtime = None
        with patch.object(Path, "read_text", side_effect=OSError("transient read failure")):
            with self.assertRaises(RuntimeError):
                adapters._executor()
            self.assertIsNone(config._cfg._cache)
        self.assertEqual(adapters._executor(), "pool")


if __name__ == "__main__":
    with tempfile.TemporaryDirectory(prefix="spendguard-lane-config-") as isolated_home:
        os.environ["SPENDGUARD_HOME"] = isolated_home
        os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"
        from spendguard import adapters, config

        with patch.dict(os.environ), patch.object(
            config, "CONFIG_JSON", Path(isolated_home) / config.CONFIG_JSON.name
        ):
            os.environ.pop("SPENDGUARD_ADVISOR_EXECUTOR", None)
            lane_config_result = unittest.main(exit=False).result
            config.cfg_invalidate()
    sys.exit(not lane_config_result.wasSuccessful())
