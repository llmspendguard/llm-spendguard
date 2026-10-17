"""Cold-start coverage for lane-value refresh gating. Offline: no ledger or network access."""
import json
import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from spendguard import lane_value, receipt


cache_path = Path(tempfile.mkdtemp(prefix="sg-lane-value-refresh-")) / "receipt-cache.json"
stamp_calls = []
mark_calls = []

receipt._cache_path = lambda: cache_path
lane_value.stamp_from_ledger = lambda: stamp_calls.append(True) or {}
lane_value._mark_stamped = lambda: mark_calls.append(True)


def run_with_lane_value_cache(cache):
    cache_path.write_text(json.dumps(cache))
    stamp_calls.clear()
    mark_calls.clear()
    lane_value.refresh_lane_value_if_stale(max_age_s=180)
    return len(stamp_calls), len(mark_calls)


now = time.time()
assert run_with_lane_value_cache({"lane_value_asof": now}) == (1, 1)
assert run_with_lane_value_cache({"lane_value_asof": now, "routed_lane_value": {}}) == (1, 1)
assert run_with_lane_value_cache({
    "lane_value_asof": now,
    "routed_lane_value": {"gemini": {"by_day": {"2026-10-16": 0.25}}},
}) == (0, 0)
assert run_with_lane_value_cache({
    "lane_value_asof": now - 181,
    "routed_lane_value": {"gemini": {"by_day": {"2026-10-16": 0.25}}},
}) == (1, 1)
print("ok — cold-start refresh, fresh-value fast path, and stale refresh")
