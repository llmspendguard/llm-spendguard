"""Offline complete-response, fault/concurrency and mutation guards for plan readers."""
import concurrent.futures
import copy
import io
import json
import os
import pathlib
import tempfile
import threading
import urllib.parse
from unittest.mock import patch

from spendguard import kimi_exec, zai_exec, subscription_headroom_surfaces as surfaces

FIXTURE_RATIO = 0.25
FIXTURE_REMAINING = 75
FIXTURE_PERCENT = 25
FIXTURE_RESET = "2099-01-01T00:00:00Z"
FIXTURE_MILLISECONDS = 4070908800000
FIXTURE_TIMEOUT = 1
FIXTURE_TTL = 300
FIXTURE_CONCURRENT_CALLERS = 8
FIXTURE_SERVER_HOST = "localhost"
FIXTURE_SERVER_PORT = 43210
FIXTURE_TOKEN = os.path.basename(__file__)
FIXTURE_SOURCE = __file__
FIXTURE_URL = "file:" + __file__
FIXTURE_SETTINGS = {"url": FIXTURE_URL, "source": FIXTURE_SOURCE, "auth_prefix": "", "quota_types": ["CREDIT_LIMIT"]}
KIMI_PAYLOAD = {"data": {"kind": "ok", "quota": {"usages": {
    "window": {"usedRatio": FIXTURE_RATIO, "resetAt": FIXTURE_RESET}}}}}
ZAI_PAYLOAD = {"success": True, "data": {"limits": [
    {"type": "CREDIT_LIMIT", "percentage": FIXTURE_PERCENT, "nextResetTime": FIXTURE_MILLISECONDS}]}}


def assert_subscription_surface_guard():
    kimi = surfaces.parse_kimi_subscription_surface(copy.deepcopy(KIMI_PAYLOAD), FIXTURE_SETTINGS)
    zai = surfaces.parse_zai_subscription_surface(copy.deepcopy(ZAI_PAYLOAD), FIXTURE_SETTINGS)
    assert kimi[0]["remaining_pct"] == zai[0]["remaining_pct"] == FIXTURE_REMAINING
    assert kimi[0]["reset_ts"] == zai[0]["reset_ts"] == FIXTURE_MILLISECONDS / surfaces.SURFACE_MILLISECONDS_PER_SECOND
    with patch.object(surfaces.urllib.request, "urlopen", side_effect=TimeoutError):
        report = surfaces.read_subscription_surface_report(FIXTURE_SETTINGS, FIXTURE_TOKEN, FIXTURE_TIMEOUT,
                                                           surfaces.parse_zai_subscription_surface)
    assert report["buckets"] is None and report["reason"] == "usage_read_gap:TimeoutError"
    assert report["uncertainty"] == "provider_data_unknown" and report["window"] is None and report["n"] == 0


def assert_subscription_surface_complete_response():
    # A malformed late bucket must invalidate the WHOLE response, never return the valid prefix.
    payload = copy.deepcopy(ZAI_PAYLOAD)
    payload["data"]["limits"].append({"type": "CREDIT_LIMIT"})
    with patch.object(surfaces.urllib.request, "urlopen", return_value=io.BytesIO(json.dumps(payload).encode())):
        report = surfaces.read_subscription_surface_report(FIXTURE_SETTINGS, FIXTURE_TOKEN, FIXTURE_TIMEOUT,
                                                           surfaces.parse_zai_subscription_surface)
    assert report["buckets"] is None and report["reason"] == "usage_read_gap:KeyError"
    for value in (float("nan"), float("inf"), True, -FIXTURE_PERCENT):
        bad = copy.deepcopy(KIMI_PAYLOAD)
        bad["data"]["quota"]["usages"]["window"]["usedRatio"] = value
        try:
            surfaces.parse_kimi_subscription_surface(bad, FIXTURE_SETTINGS)
        except surfaces.SubscriptionHeadroomDataGap:
            pass
        else:
            raise AssertionError("invalid usage accepted")


def assert_subscription_surface_wiring():
    report = {"buckets": [{"bucket": "fixture", "remaining_pct": FIXTURE_REMAINING, "reset_ts": None}],
              "reason": None, "source": FIXTURE_SOURCE}
    with (patch.object(kimi_exec, "discover_kimi_server_surface",
                       return_value=(FIXTURE_SETTINGS, FIXTURE_TOKEN, None)),
          patch.object(surfaces, "cached_subscription_surface_report", return_value=report)):
        assert kimi_exec.usage_report() == report
        assert kimi_exec.usage() == report["buckets"]
    with patch.object(surfaces, "cached_subscription_surface_report", return_value=report):
        assert zai_exec.usage_report() == report
        assert zai_exec.usage() == report["buckets"]
    with patch.dict(os.environ, {"SPENDGUARD_KIMI_USAGE_URL": FIXTURE_URL}):
        assert surfaces.subscription_surface_settings("kimi_code", "SPENDGUARD_KIMI_USAGE_URL")["url"] == FIXTURE_URL
    unknown_settings = {**FIXTURE_SETTINGS, "url": None}
    with patch.object(surfaces.urllib.request, "urlopen", side_effect=AssertionError("must not probe")):
        unknown = surfaces.read_subscription_surface_report(unknown_settings, FIXTURE_TOKEN, FIXTURE_TIMEOUT, None)
    assert unknown["buckets"] is None and unknown["reason"] == "usage_url_not_configured"


def assert_kimi_runtime_surface_discovery():
    settings = {**FIXTURE_SETTINGS, "url": None, "path": "/fixture/usage"}
    with tempfile.TemporaryDirectory() as temporary_directory:
        kimi_home = pathlib.Path(temporary_directory)
        instance_directory = (kimi_home / kimi_exec.KIMI_SERVER_DIRECTORY_NAME /
                              kimi_exec.KIMI_SERVER_INSTANCES_DIRECTORY_NAME)
        instance_directory.mkdir(parents=True)
        instance_file = instance_directory / (FIXTURE_TOKEN + kimi_exec.KIMI_SERVER_INSTANCE_SUFFIX)
        instance_file.write_text(json.dumps({"pid": FIXTURE_TIMEOUT, "host": FIXTURE_SERVER_HOST,
                                             "port": FIXTURE_SERVER_PORT, "startedAt": FIXTURE_TIMEOUT}))
        (kimi_home / kimi_exec.KIMI_SERVER_TOKEN_FILENAME).write_text(FIXTURE_TOKEN)
        resolved, token, gap = kimi_exec.discover_kimi_server_surface(
            settings, kimi_home=kimi_home, process_exists=lambda _pid: True)
        expected_url = urllib.parse.urlunsplit((kimi_exec.KIMI_SERVER_URL_SCHEME,
                                               f"{FIXTURE_SERVER_HOST}:{FIXTURE_SERVER_PORT}",
                                               settings["path"], "", ""))
        assert resolved["url"] == expected_url
        assert token == FIXTURE_TOKEN and gap is None
        with patch.object(surfaces.urllib.request, "urlopen",
                          return_value=io.BytesIO(json.dumps(KIMI_PAYLOAD).encode())):
            report = surfaces.read_subscription_surface_report(
                resolved, token, FIXTURE_TIMEOUT, surfaces.parse_kimi_subscription_surface)
        assert report["buckets"][0]["remaining_pct"] == FIXTURE_REMAINING

    unresolved, token, gap = kimi_exec.discover_kimi_server_surface(
        settings, kimi_home=kimi_home, process_exists=lambda _pid: True)
    assert unresolved["url"] is None and token is None and gap == "kimi_server_not_running"
    with (patch.object(kimi_exec, "discover_kimi_server_surface",
                       return_value=(unresolved, token, gap)),
          patch.object(surfaces.urllib.request, "urlopen", side_effect=AssertionError("must not probe"))):
        unknown = kimi_exec.usage_report()
    assert unknown["buckets"] is None and unknown["reason"] == "kimi_server_not_running"
    assert unknown["uncertainty"] == "provider_data_unknown"


def assert_subscription_surface_concurrency():
    cache = {}
    count = []
    started = threading.Event()
    release = threading.Event()

    def subscription_surface_fault_fetch():
        count.append(True)
        started.set()
        assert release.wait(FIXTURE_TIMEOUT)
        return {"buckets": None, "reason": "injected_slow_probe"}

    with concurrent.futures.ThreadPoolExecutor(max_workers=FIXTURE_CONCURRENT_CALLERS) as pool:
        pending = [pool.submit(surfaces.cached_subscription_surface_report, cache, FIXTURE_TTL,
                               subscription_surface_fault_fetch) for _ in range(FIXTURE_CONCURRENT_CALLERS)]
        assert started.wait(FIXTURE_TIMEOUT)
        release.set()
        reports = [future.result() for future in pending]
    assert len(count) == 1 and all(report["reason"] == "injected_slow_probe" for report in reports)
    reports[0]["reason"] = "caller_mutation"
    assert cache["report"]["reason"] == "injected_slow_probe"


def assert_subscription_surface_mutation_proof():
    # Reintroduce the old no-probe reader and deliberately wrong percentage conversion.
    mutations = (
        (surfaces, "parse_kimi_subscription_surface", lambda *_: None, assert_subscription_surface_guard),
        (surfaces, "parse_zai_subscription_surface", lambda *_: None, assert_subscription_surface_guard),
        (surfaces, "subscription_surface_remaining", lambda *_: surfaces.SURFACE_PERCENT_SCALE, assert_subscription_surface_guard),
        (surfaces, "read_subscription_surface_report", lambda *_: {"buckets": [], "reason": None}, assert_subscription_surface_guard),
        (kimi_exec, "usage", lambda: None, assert_subscription_surface_wiring),
        (zai_exec, "usage", lambda: None, assert_subscription_surface_wiring),
    )
    for target, symbol, wrong, guard in mutations:
        with patch.object(target, symbol, wrong):
            try:
                guard()
            except (AssertionError, TypeError, KeyError):
                pass
            else:
                raise AssertionError(f"mutation survived: {symbol}")
    assert_subscription_surface_guard()
    assert_subscription_surface_wiring()


if __name__ == "__main__":
    assert_subscription_surface_guard()
    assert_subscription_surface_complete_response()
    assert_subscription_surface_wiring()
    assert_kimi_runtime_surface_discovery()
    assert_subscription_surface_concurrency()
    assert_subscription_surface_mutation_proof()
    print("OK subscription headroom: complete response, fault, concurrency, six mutation guards")
