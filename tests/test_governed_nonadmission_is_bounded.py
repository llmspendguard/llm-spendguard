"""Offline bounded repro: governed non-admission returns a typed deadline result without dispatching."""
import os
import sys
import tempfile
import time

os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="sg-governed-bound-")
os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"
os.environ["SPENDGUARD_NO_AUTOINSTALL"] = "1"
os.environ["OPENAI_API_KEY"] = "test-key-not-real"
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from spendguard import adapters, dispatch

failures = []


def assert_governed_bound(label, condition):
    print(f"  [{'OK' if condition else 'FAIL'}] {label}")
    if not condition:
        failures.append(label)


original_admit = dispatch.admit
original_guarded = adapters._call_guarded
dispatched = []
try:
    dispatch.admit = lambda vendor, model, deadline_s, **kwargs: dispatch._Admission(
        False, False, False, vendor, model, False, error="offline non-admissible")
    adapters._call_guarded = lambda *args, **kwargs: dispatched.append(True)
    adapters._resolve_guard.on = True
    started = time.monotonic()
    result = adapters.call("openai:test-model", "offline", intent="governed-bound", governed=True, timeout_s=0.05)
    elapsed = time.monotonic() - started
    assert_governed_bound("non-admissible governed call returns within its explicit bound", elapsed < 0.5)
    assert_governed_bound("result is the documented typed deadline dictionary",
                          result.get("text") is None and "deadline_exceeded (governed)" in result.get("error", ""))
    assert_governed_bound("provider dispatch never runs after refusal", not dispatched)
finally:
    dispatch.admit = original_admit
    adapters._call_guarded = original_guarded
    adapters._resolve_guard.on = False

print(f"\n{'OK' if not failures else 'FAIL'} test_governed_nonadmission_is_bounded: {len(failures)} failure(s)")
sys.exit(1 if failures else 0)
