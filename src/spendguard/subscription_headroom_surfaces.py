"""Read documented subscription status surfaces, never inference or metered APIs.

Endpoints are config, with vendor provenance in the packaged JSON. Kimi's endpoint
is a local CLI server URL supplied by its operator; this reader never starts one.
Every unusable response produces a named unknown report, never fabricated quota.
"""
import datetime
import copy
import importlib.resources
import json
import math
import os
import threading
import time
import urllib.request

SURFACE_CONFIG_RESOURCE = "subscription_headroom_surfaces.json"
SURFACE_PERCENT_SCALE = 100
SURFACE_RATIO_MINIMUM = 0
SURFACE_RATIO_MAXIMUM = 1
SURFACE_UNKNOWN_SAMPLE_COUNT = 0
SURFACE_MILLISECONDS_PER_SECOND = 1000
_surface_report_lock = threading.RLock()


class SubscriptionHeadroomDataGap(ValueError):
    """A complete provider response cannot establish complete headroom."""


def subscription_surface_settings(section, url_env):
    from . import config
    defaults = json.loads(importlib.resources.files(__package__).joinpath(SURFACE_CONFIG_RESOURCE).read_text())[section]
    return {**defaults, "url": os.environ.get(url_env) or config._cfg_get(section, "usage_url", defaults["url"])}


def subscription_surface_remaining(value, scale):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise SubscriptionHeadroomDataGap("invalid_usage_value")
    ratio = value / scale
    if not SURFACE_RATIO_MINIMUM <= ratio <= SURFACE_RATIO_MAXIMUM:
        raise SubscriptionHeadroomDataGap("usage_value_out_of_range")
    return math.floor((SURFACE_RATIO_MAXIMUM - ratio) * SURFACE_PERCENT_SCALE)


def subscription_surface_reset(timestamp):
    if timestamp is None:
        return None
    if not isinstance(timestamp, str):
        raise SubscriptionHeadroomDataGap("invalid_reset_timestamp")
    parsed = datetime.datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise SubscriptionHeadroomDataGap("reset_timezone_missing")
    return parsed.timestamp()


def parse_kimi_subscription_surface(payload, settings):
    data = payload.get("data", payload)
    if data.get("kind") != "ok":
        raise SubscriptionHeadroomDataGap("kimi_usage_not_ok")
    usages = data["quota"]["usages"]
    if not isinstance(usages, dict) or not usages:
        raise SubscriptionHeadroomDataGap("kimi_quota_windows_missing")
    return [{"bucket": name, "remaining_pct": subscription_surface_remaining(row["usedRatio"], SURFACE_RATIO_MAXIMUM),
             "reset_ts": subscription_surface_reset(row.get("resetAt"))} for name, row in usages.items()]


def parse_zai_subscription_surface(payload, settings):
    if payload.get("success") is not True:
        raise SubscriptionHeadroomDataGap("zai_usage_not_successful")
    rows = payload["data"]["limits"]
    if not isinstance(rows, list) or not rows:
        raise SubscriptionHeadroomDataGap("zai_quota_windows_missing")
    buckets = []
    for index, row in enumerate(rows):
        if row["type"] not in settings["quota_types"]:
            # The vendor protocol distinguishes tool-call limits from plan token/credit limits.
            continue
        buckets.append({"bucket": f"{row['type']}:{index}",
                        "remaining_pct": subscription_surface_remaining(row["percentage"], SURFACE_PERCENT_SCALE),
                        "reset_ts": subscription_zai_reset_time(row.get("nextResetTime"))})
    if not buckets:
        raise SubscriptionHeadroomDataGap("zai_plan_windows_missing")
    return buckets


def subscription_zai_reset_time(timestamp):
    """Observed monitor nextResetTime is Unix milliseconds; absent stays unknown."""
    if timestamp is None:
        return None
    if isinstance(timestamp, bool) or not isinstance(timestamp, (int, float)) or not math.isfinite(timestamp):
        raise SubscriptionHeadroomDataGap("invalid_zai_reset_timestamp")
    return timestamp / SURFACE_MILLISECONDS_PER_SECOND


def read_subscription_surface_report(settings, token, timeout, parser):
    report = {"buckets": None, "source": settings["source"], "reason": None,
              "n": SURFACE_UNKNOWN_SAMPLE_COUNT, "window": None, "uncertainty": "provider_data_unknown"}
    if not settings.get("url"):
        report["reason"] = "usage_url_not_configured"
        return report
    if not token:
        report["reason"] = "usage_auth_not_configured"
        return report
    from . import config
    try:
        request = urllib.request.Request(settings["url"], headers={"Authorization": settings["auth_prefix"] + token})
        with urllib.request.urlopen(request, context=config.ssl_context(), timeout=timeout) as response:
            payload = json.load(response)
        report["buckets"] = parser(payload, settings)
        report["uncertainty"] = "provider_reported_reset_unknown" if any(
            bucket["reset_ts"] is None for bucket in report["buckets"]) else "provider_reported"
    except Exception as exc:
        # No exception prose: URLs and authentication material can occur in it.
        report["reason"] = f"usage_read_gap:{type(exc).__name__}"
    return report


def cached_subscription_surface_report(cache, ttl, fetch):
    with _surface_report_lock:
        now = time.time()
        report = cache.get("report")
        resets = [bucket["reset_ts"] for bucket in (report or {}).get("buckets") or [] if bucket["reset_ts"]]
        if report is not None and now - cache.get("at", 0) < ttl and not any(now >= reset for reset in resets):
            return copy.deepcopy(report)
        report = fetch()
        cache.update(at=now, report=report)
        return copy.deepcopy(report)
