"""Unknown-price faults stay named and isolated across concurrent integrity scans."""
import copy
from concurrent.futures import ThreadPoolExecutor
import io
import logging
import os
import tempfile
from unittest.mock import patch

os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="sg-unpriceable-integrity-")
os.environ["SPENDGUARD_NO_AUTOINSTALL"] = "1"

from spendguard import budget, cost_integrity, model_catalog, pricing, receipt

INTEGRITY_UNKNOWN_MODEL = "fixture-unpriceable-realtime-model"
INTEGRITY_KNOWN_MODEL = "fixture-priced-realtime-model"
INTEGRITY_FAULT_DAY = "fixture-day"
INTEGRITY_FAULT_KIND = "realtime"
INTEGRITY_FAULT_CALLS = 1
INTEGRITY_FAULT_TOKENS = 1
INTEGRITY_ZERO_COST = 0.0
INTEGRITY_CONCURRENT_SCANS = 4
INTEGRITY_RECONSTRUCTION_GAP = "token_cost_reconstruction_unavailable"
INTEGRITY_TRACEBACK_MARKER = "Traceback"
INTEGRITY_SYNTHETIC_MODEL = "stub-vlm"
INTEGRITY_VERIFIED_FLAG = "--verified"
INTEGRITY_CATALOG_REMEDY_MARKER = "model catalog SSOT"
INTEGRITY_FAULT_ROWS = tuple(
    (model, INTEGRITY_FAULT_DAY, INTEGRITY_FAULT_KIND, INTEGRITY_FAULT_CALLS,
     INTEGRITY_ZERO_COST, INTEGRITY_FAULT_TOKENS, INTEGRITY_FAULT_TOKENS)
    for model in (INTEGRITY_UNKNOWN_MODEL, INTEGRITY_KNOWN_MODEL))


def inject_realtime_integrity_unknown_price(model, *args, **kwargs):
    if model == INTEGRITY_UNKNOWN_MODEL:
        raise pricing.PriceUnknownError(model)
    return INTEGRITY_ZERO_COST


def run_concurrent_unpriceable_integrity_scan(_scan):
    gaps = []
    findings, unpriced = cost_integrity.classify_impossible_cost_buckets(
        iter(INTEGRITY_FAULT_ROWS), gaps=gaps)
    assert findings == [] and unpriced == INTEGRITY_FAULT_CALLS
    unknown_gaps = [gap for gap in gaps if gap["gap"] == INTEGRITY_RECONSTRUCTION_GAP]
    assert len(unknown_gaps) == INTEGRITY_FAULT_CALLS
    assert unknown_gaps[0]["model"] == INTEGRITY_UNKNOWN_MODEL
    assert INTEGRITY_UNKNOWN_MODEL in unknown_gaps[0]["error"]
    assert INTEGRITY_TRACEBACK_MARKER not in unknown_gaps[0]["error"]
    return gaps


integrity_original_rows = copy.deepcopy(INTEGRITY_FAULT_ROWS)
integrity_log_output = io.StringIO()
integrity_log_handler = logging.StreamHandler(integrity_log_output)
cost_integrity.COST_INTEGRITY_LOGGER.addHandler(integrity_log_handler)
try:
    with patch.object(pricing, "realtime_cost", side_effect=inject_realtime_integrity_unknown_price) as integrity_price:
        with ThreadPoolExecutor(max_workers=INTEGRITY_CONCURRENT_SCANS) as integrity_scan_pool:
            integrity_scan_results = list(integrity_scan_pool.map(
                run_concurrent_unpriceable_integrity_scan, range(INTEGRITY_CONCURRENT_SCANS)))
    assert len(integrity_scan_results) == INTEGRITY_CONCURRENT_SCANS
    assert any(call.args[0] == INTEGRITY_KNOWN_MODEL for call in integrity_price.call_args_list)
    assert INTEGRITY_TRACEBACK_MARKER not in integrity_log_output.getvalue()
    assert INTEGRITY_FAULT_ROWS == integrity_original_rows
finally:
    cost_integrity.COST_INTEGRITY_LOGGER.removeHandler(integrity_log_handler)
    integrity_log_handler.close()
print("OK concurrent realtime unknown prices remain named gaps without traceback or row mutation")

integrity_receipt_rows = [{"model": model, "calls": INTEGRITY_FAULT_CALLS}
                          for model in (INTEGRITY_SYNTHETIC_MODEL, INTEGRITY_UNKNOWN_MODEL)]
assert model_catalog.billing_exempt(INTEGRITY_SYNTHETIC_MODEL)
with patch.object(budget, "unpriced_since", return_value=integrity_receipt_rows):
    integrity_receipt_text = "\n".join(receipt._unpriced_lines())
assert INTEGRITY_SYNTHETIC_MODEL not in integrity_receipt_text
assert INTEGRITY_UNKNOWN_MODEL in integrity_receipt_text
assert INTEGRITY_VERIFIED_FLAG in integrity_receipt_text
assert INTEGRITY_CATALOG_REMEDY_MARKER in integrity_receipt_text
print("OK synthetic calls are excluded and unpriced receipt points at the catalog writer")

INTEGRITY_PENDING_REPRICE_GAP = "pending_reprice_coverage"
INTEGRITY_REPRICE_REMEDY = "spendguard reprice"
integrity_pending_rows = integrity_receipt_rows + [
    {"model": INTEGRITY_KNOWN_MODEL, "calls": INTEGRITY_FAULT_CALLS}]
integrity_pending_original = copy.deepcopy(integrity_pending_rows)
with patch.object(budget, "unpriced_since", return_value=integrity_pending_rows), \
        patch.object(pricing, "price", side_effect=inject_realtime_integrity_unknown_price):
    integrity_pending_lines = receipt._unpriced_lines()
integrity_pending_text = "\n".join(integrity_pending_lines)
assert INTEGRITY_PENDING_REPRICE_GAP in integrity_pending_text
assert INTEGRITY_REPRICE_REMEDY in integrity_pending_text
assert INTEGRITY_KNOWN_MODEL in integrity_pending_text
assert INTEGRITY_UNKNOWN_MODEL in integrity_pending_text
assert INTEGRITY_SYNTHETIC_MODEL not in integrity_pending_text
assert integrity_pending_rows == integrity_pending_original
integrity_unknown_lines = integrity_pending_lines[:integrity_pending_lines.index(next(
    line for line in integrity_pending_lines if INTEGRITY_PENDING_REPRICE_GAP in line))]
assert all(INTEGRITY_KNOWN_MODEL not in line for line in integrity_unknown_lines)
print("OK historical unpriced flags retain pending reprice coverage separately from current unknown prices")
