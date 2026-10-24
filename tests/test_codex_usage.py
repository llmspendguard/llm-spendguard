"""Offline guards for Codex's installed account/rateLimits/read protocol."""
import json
import os
import sys
import tempfile
from unittest.mock import patch

os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="sg-codexq-")
os.environ.setdefault("SPENDGUARD_TEST_ISOLATED", "1")
os.environ.setdefault("SPENDGUARD_NO_AUTOINSTALL", "1")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from spendguard import codex_exec as codex_lane  # noqa: E402
from spendguard import config as spendguard_config  # noqa: E402

FIXTURE_USED_PERCENT = 12
FIXTURE_REMAINING_PERCENT = 88
FIXTURE_RESET_TIMESTAMP = 4070908800
FIXTURE_WINDOW_MINUTES = 300
FIXTURE_LIMIT_ID = "fixture-limit"
FIXTURE_LIMIT_NAME = "Fixture limit"
FIXTURE_SOCKET_PATH = os.path.join(tempfile.gettempdir(), "codex-fixture.sock")
FIXTURE_EXECUTABLE_PATH = os.path.join(tempfile.gettempdir(), "codex-fixture")
FIXTURE_INVALID_USED_PERCENT = 101
FIXTURE_RESPONSE = {
    "rateLimits": None,
    "rateLimitsByLimitId": {
        FIXTURE_LIMIT_ID: {
            "limitId": FIXTURE_LIMIT_ID,
            "limitName": FIXTURE_LIMIT_NAME,
            "primary": {
                "usedPercent": FIXTURE_USED_PERCENT,
                "resetsAt": FIXTURE_RESET_TIMESTAMP,
                "windowDurationMins": FIXTURE_WINDOW_MINUTES,
            },
            "secondary": None,
        }
    },
}


def assert_codex_rate_limits_protocol_parser():
    buckets = codex_lane._parse_rate_limits(FIXTURE_RESPONSE)
    assert buckets == [{
        "bucket": f"{FIXTURE_LIMIT_NAME}:primary ({FIXTURE_WINDOW_MINUTES}m window)",
        "remaining_pct": FIXTURE_REMAINING_PERCENT,
        "reset_ts": float(FIXTURE_RESET_TIMESTAMP),
    }]
    assert codex_lane._parse_rate_limits({"rateLimits": {"primary": None, "secondary": None}}) is None
    invalid = {"rateLimits": {"primary": {"usedPercent": FIXTURE_INVALID_USED_PERCENT}, "secondary": None}}
    assert codex_lane._parse_rate_limits(invalid) is None


class CodexRateLimitsCompletedProcess:
    returncode = 0
    stderr = ""
    stdout = json.dumps({"id": codex_lane.CODEX_APP_SERVER_RATE_LIMITS_REQUEST_ID,
                         "result": FIXTURE_RESPONSE}) + "\n"


def assert_codex_rate_limits_transport_report():
    with patch.object(codex_lane, "_bin", return_value=FIXTURE_EXECUTABLE_PATH), \
            patch.object(codex_lane, "_codex_home", return_value=os.path.dirname(FIXTURE_SOCKET_PATH)), \
            patch.object(codex_lane.os.path, "exists", return_value=True), \
            patch.object(spendguard_config, "lane_exec_env", return_value={}), \
            patch.object(codex_lane.subprocess, "run", return_value=CodexRateLimitsCompletedProcess()):
        report = codex_lane._codex_usage_report_uncached()
    assert report["reason"] is None
    assert report["buckets"][0]["remaining_pct"] == FIXTURE_REMAINING_PERCENT
    assert report["uncertainty"] == "provider_reported"

    with patch.object(codex_lane, "_bin", return_value=None):
        unavailable = codex_lane._codex_usage_report_uncached()
    assert unavailable["buckets"] is None
    assert unavailable["reason"] == "codex_cli_not_available"


def assert_codex_rate_limits_offline_transport_gap():
    with patch.object(codex_lane, "_bin", return_value=FIXTURE_EXECUTABLE_PATH), \
            patch.object(codex_lane, "_codex_home", return_value=os.path.dirname(FIXTURE_SOCKET_PATH)), \
            patch.object(codex_lane.os.path, "exists", return_value=True), \
            patch.object(spendguard_config, "lane_exec_env",
                         side_effect=spendguard_config.IsolatedCliLaunchError), \
            patch.object(codex_lane.subprocess, "run") as subprocess_run:
        unavailable = codex_lane._codex_usage_report_uncached()
    assert unavailable["buckets"] is None
    assert unavailable["reason"] == "codex_rate_limits_transport_failed"
    assert unavailable["uncertainty"] == "provider_data_unknown"
    subprocess_run.assert_not_called()


if __name__ == "__main__":
    assert_codex_rate_limits_protocol_parser()
    assert_codex_rate_limits_transport_report()
    assert_codex_rate_limits_offline_transport_gap()
    print("OK codex usage: current app-server schema, transport, and honest UNKNOWN")
