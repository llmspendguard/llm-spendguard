"""Unknown catalog providers preserve reconstruction evidence with explicit gaps."""
import copy
import importlib.util
import os
from pathlib import Path
import tempfile
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="pricing-provider-gap-")
os.environ.setdefault("SPENDGUARD_TEST_ISOLATED", "1")
os.environ.setdefault("SPENDGUARD_NO_AUTOINSTALL", "1")

from spendguard import pricing  # noqa: E402

PROVIDER_GAP_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "reconstruct" / "realtime_find_batch.py"
PROVIDER_GAP_MODULE = "pricing_realtime_provider_gap_fixture"
PROVIDER_GAP_UNKNOWN_MODEL = "fixture-provider-unpriced"
PROVIDER_GAP_KNOWN_PROVIDER = "fixture-provider-priced"
PROVIDER_GAP_PROJECT = "fixture-provider-project"
PROVIDER_GAP_DAY = "2026-10-01"
PROVIDER_GAP_UNKNOWN_USD = 2.0
PROVIDER_GAP_KNOWN_USD = 3.0
PROVIDER_GAP_TRUTH_USD = 7.0
PROVIDER_GAP_CONCURRENT_SCANS = 4
provider_gap_spec = importlib.util.spec_from_file_location(PROVIDER_GAP_MODULE, PROVIDER_GAP_SCRIPT)
provider_gap_module = importlib.util.module_from_spec(provider_gap_spec)
provider_gap_spec.loader.exec_module(provider_gap_module)
provider_gap_runs = [
    dict(model=PROVIDER_GAP_UNKNOWN_MODEL, usd=PROVIDER_GAP_UNKNOWN_USD,
         project=PROVIDER_GAP_PROJECT, day=PROVIDER_GAP_DAY, basis="printed"),
    dict(provider=PROVIDER_GAP_KNOWN_PROVIDER, usd=PROVIDER_GAP_KNOWN_USD,
         project=PROVIDER_GAP_PROJECT, day=PROVIDER_GAP_DAY, basis="printed"),
]
provider_gap_truth = [dict(provider=PROVIDER_GAP_KNOWN_PROVIDER,
                           day=PROVIDER_GAP_DAY, usd=PROVIDER_GAP_TRUTH_USD)]
provider_gap_original = copy.deepcopy(provider_gap_runs)


def run_pricing_provider_gap_fault_scan(_scan_index):
    payload = provider_gap_module._reconstruction_cache_payload(
        provider_gap_runs, provider_gap_truth, PROVIDER_GAP_DAY)
    assert len(payload["rows"]) == len(provider_gap_runs)
    assert payload["total"] == PROVIDER_GAP_UNKNOWN_USD + PROVIDER_GAP_KNOWN_USD
    assert payload["provider_months_complete"] is False
    assert payload["rows"][0]["provider"] is None
    assert payload["rows"][1]["provider"] == PROVIDER_GAP_KNOWN_PROVIDER
    gap = next(gap for gap in payload["gaps"] if gap["kind"] == "provider_price_lookup_unavailable")
    assert gap["model"] == PROVIDER_GAP_UNKNOWN_MODEL
    assert gap["row"] == 0
    assert "PriceUnknownError" in gap["error"]
    assert all(comparison["residual"] is None for comparison in payload["provider_months"])
    assert provider_gap_runs == provider_gap_original


with patch.object(pricing, "price", side_effect=pricing.PriceUnknownError(PROVIDER_GAP_UNKNOWN_MODEL)):
    with ThreadPoolExecutor(max_workers=PROVIDER_GAP_CONCURRENT_SCANS) as provider_gap_executor:
        list(provider_gap_executor.map(run_pricing_provider_gap_fault_scan,
                                       range(PROVIDER_GAP_CONCURRENT_SCANS)))
print("OK: unknown provider preserves every row, names coverage gap, suppresses incomplete residuals concurrently")
