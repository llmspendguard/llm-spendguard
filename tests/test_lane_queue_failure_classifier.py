"""Durable lane-queue failure classification: transient deadlines retry; deterministic model refusals do not."""
import os
import sys
import tempfile

os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="spendguard-queue-classifier-")
os.environ.setdefault("SPENDGUARD_TEST_ISOLATED", "1")
os.environ.setdefault("SPENDGUARD_NO_AUTOINSTALL", "1")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from _script_checks import record_script_check
from spendguard.generate.execution import vendor_call

fails = []

record_script_check(
    fails,
    "a deadline is transient and therefore spends the durable retry budget",
    vendor_call.DEADLINE_EXCEEDED in vendor_call.RETRYABLE,
)
record_script_check(
    fails,
    "a deterministic provider payload refusal is not retried",
    vendor_call.PAYLOAD_REJECTED not in vendor_call.RETRYABLE,
)

print(f"{'[FAIL]' if fails else 'OK'} test_lane_queue_failure_classifier: {len(fails)} failure(s)")
sys.exit(1 if fails else 0)
