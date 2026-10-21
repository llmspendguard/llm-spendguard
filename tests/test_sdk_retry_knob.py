"""Offline TEST/OPS SDK retry override; no clients or network calls needed."""
import os
import sys
import tempfile
from unittest.mock import patch

if not os.environ.get("SPENDGUARD_TEST_ISOLATED"):
    os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"
    os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="spendguard-sdk-retry-")
    os.execv(sys.executable, [sys.executable] + sys.argv)

from spendguard.adapters import _sdk_client_max_retries


def test_sdk_retry_override_contract():
    retry_env_name = "SPENDGUARD_SDK_MAX_RETRIES"
    retry_timeout_s = 1
    retry_failures = []
    retry_cases = (
        ("0", None, 0),
        ("0", retry_timeout_s, 0),
        (None, retry_timeout_s, 0),
        (None, None, None),
        (None, 0, None),
        ("3", None, 3),
        ("3", retry_timeout_s, 3),
    )
    with patch.dict(os.environ):
        for retry_env_value, retry_timeout, retry_expected in retry_cases:
            if retry_env_value is None:
                os.environ.pop(retry_env_name, None)
            else:
                os.environ[retry_env_name] = retry_env_value
            retry_label = f"env={retry_env_value!r}, timeout_s={retry_timeout!r} -> {retry_expected!r}"
            try:
                assert _sdk_client_max_retries(retry_timeout) == retry_expected
            except Exception as retry_error:
                retry_failures.append(retry_label)
                print(f"[FAIL] {retry_label}: {retry_error}")
            else:
                print(f"[OK] {retry_label}")
        for retry_invalid_value in ("-1", "x", ""):
            os.environ[retry_env_name] = retry_invalid_value
            for retry_timeout in (None, retry_timeout_s):
                retry_label = f"env={retry_invalid_value!r}, timeout_s={retry_timeout!r} raises clear ValueError"
                try:
                    _sdk_client_max_retries(retry_timeout)
                except ValueError as retry_error:
                    if retry_env_name in str(retry_error) and "non-negative integer" in str(retry_error):
                        print(f"[OK] {retry_label}")
                        continue
                retry_failures.append(retry_label)
                print(f"[FAIL] {retry_label}")
    return bool(retry_failures)


if __name__ == "__main__":
    sys.exit(test_sdk_retry_override_contract())
