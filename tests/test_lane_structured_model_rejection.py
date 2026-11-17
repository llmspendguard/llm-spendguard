"""Structured provider model refusals become lane-admission knowledge; prose cannot poison it."""
import os
import sys
import tempfile

os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="spendguard-lane-rejection-")
os.environ.setdefault("SPENDGUARD_TEST_ISOLATED", "1")
os.environ.setdefault("SPENDGUARD_NO_AUTOINSTALL", "1")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from _script_checks import record_script_check
from spendguard.analyze.measurement import lane_servability
from spendguard.store.reference_data import catalog

fails = []
lane = "fixture-lane"
rejected_model = "fixture-unserved-model"
prose_only_model = "fixture-prose-only"

record_script_check(
    fails,
    "structured model_not_found is persisted for admission",
    lane_servability.record_structured_lane_rejection(
        lane, rejected_model, {"status_code": 400, "error_code": "model_not_found", "error": "fixture refusal"}
    ),
)
record_script_check(
    fails,
    "the rejected model is refused by the next admission lookup",
    rejected_model in set(catalog.lane_unserved_ids(lane) or []),
)
record_script_check(
    fails,
    "unstructured refusal prose cannot poison lane servability",
    not lane_servability.record_structured_lane_rejection(
        lane, prose_only_model, {"status_code": 400, "error": "not supported"}
    ) and prose_only_model not in set(catalog.lane_unserved_ids(lane) or []),
)

print(f"{'[FAIL]' if fails else 'OK'} test_lane_structured_model_rejection: {len(fails)} failure(s)")
sys.exit(1 if fails else 0)
