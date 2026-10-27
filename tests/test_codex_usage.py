"""Offline guards for Codex's installed account/rateLimits/read protocol."""
import os
import sys
import tempfile
from unittest.mock import patch

os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="sg-codexq-")
os.environ.setdefault("SPENDGUARD_TEST_ISOLATED", "1")
os.environ.setdefault("SPENDGUARD_NO_AUTOINSTALL", "1")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from spendguard import codex_exec as codex_lane  # noqa: E402
from spendguard import codex_daemon  # noqa: E402

FIXTURE_USED_PERCENT = 12
FIXTURE_REMAINING_PERCENT = 88
FIXTURE_RESET_TIMESTAMP = 4070908800
FIXTURE_WINDOW_MINUTES = 300
FIXTURE_LIMIT_ID = "fixture-limit"
FIXTURE_LIMIT_NAME = "Fixture limit"
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


def assert_codex_rate_limits_transport_report():
    with patch.object(codex_daemon, "read_rate_limits",
                      return_value={"rate_limits": FIXTURE_RESPONSE, "error": None}):
        report = codex_lane._codex_usage_report_uncached()
    assert report["reason"] is None
    assert report["buckets"][0]["remaining_pct"] == FIXTURE_REMAINING_PERCENT
    assert report["uncertainty"] == "provider_reported"

    with patch.object(codex_daemon, "read_rate_limits",
                      return_value={"rate_limits": None, "error": "daemon down"}):
        unavailable = codex_lane._codex_usage_report_uncached()
    assert unavailable["buckets"] is None
    assert unavailable["reason"] == "codex_warm_pool_rate_limits_unavailable"


def assert_codex_rate_limits_offline_transport_gap():
    with patch.object(codex_daemon, "read_rate_limits", side_effect=RuntimeError("request failed")):
        unavailable = codex_lane._codex_usage_report_uncached()
    assert unavailable["buckets"] is None
    assert unavailable["reason"] == "codex_warm_pool_rate_limits_request_failed"
    assert unavailable["uncertainty"] == "provider_data_unknown"

    for response, reason in (
            ({"rate_limits": "malformed", "error": None}, "codex_warm_pool_rate_limits_response_malformed"),
            ({"rate_limits": {}, "error": None}, "codex_rate_limits_windows_missing")):
        with patch.object(codex_daemon, "read_rate_limits", return_value=response):
            unavailable = codex_lane._codex_usage_report_uncached()
        assert unavailable["buckets"] is None
        assert unavailable["reason"] == reason
        assert unavailable["uncertainty"] == "provider_data_unknown"


if __name__ == "__main__":
    assert_codex_rate_limits_protocol_parser()
    assert_codex_rate_limits_transport_report()
    assert_codex_rate_limits_offline_transport_gap()
    print("OK codex usage: current app-server schema, transport, and honest UNKNOWN")
