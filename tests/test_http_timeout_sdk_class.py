"""Regression checks for SDK-native HTTP timeout construction."""
import os
import sys
import tempfile

if not os.environ.get("SPENDGUARD_TEST_ISOLATED"):
    os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"
    os.environ["SPENDGUARD_NO_AUTOINSTALL"] = "1"
    os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(
        prefix="spendguard-http-timeout-sdk-class-"
    )
    os.execv(sys.executable, [sys.executable] + sys.argv)

import anthropic
import httpx
import openai

from spendguard.adapters import CONNECT_TIMEOUT_S, _http_timeout


HTTP_TIMEOUT_TEST_BUDGET_S = 30.0
HTTP_TIMEOUT_TEST_API_KEY = "sk-test-offline"
HTTP_TIMEOUT_TEST_MAX_RETRIES = 0


def check_sdk_timeout_class_condition(check_name, condition):
    print(("  [OK] " if condition else "  [FAIL] ") + check_name)
    return 0 if condition else 1


http_timeout_test_failures = 0
for sdk_name, sdk_base_client in (
    ("openai", openai._base_client),
    ("anthropic", anthropic._base_client),
):
    sdk_timeout = _http_timeout(HTTP_TIMEOUT_TEST_BUDGET_S, sdk_base_client)
    http_timeout_test_failures += check_sdk_timeout_class_condition(
        f"{sdk_name} timeout does not use standalone httpx.Timeout",
        not isinstance(sdk_timeout, httpx.Timeout),
    )
    http_timeout_test_failures += check_sdk_timeout_class_condition(
        f"{sdk_name} connect timeout preserves the fast-connect budget",
        sdk_timeout.connect == min(CONNECT_TIMEOUT_S, HTTP_TIMEOUT_TEST_BUDGET_S),
    )
    http_timeout_test_failures += check_sdk_timeout_class_condition(
        f"{sdk_name} read timeout preserves the full request budget",
        sdk_timeout.read == HTTP_TIMEOUT_TEST_BUDGET_S,
    )

try:
    anthropic.Anthropic(
        api_key=HTTP_TIMEOUT_TEST_API_KEY,
        timeout=_http_timeout(HTTP_TIMEOUT_TEST_BUDGET_S, anthropic._base_client),
        max_retries=HTTP_TIMEOUT_TEST_MAX_RETRIES,
    )
except TypeError as anthropic_timeout_type_error:
    print(
        "  [FAIL] anthropic accepts its SDK-native timeout at construction: "
        f"{anthropic_timeout_type_error}"
    )
    http_timeout_test_failures += 1
else:
    print("  [OK] anthropic accepts its SDK-native timeout at construction")

print(
    ("[OK]" if not http_timeout_test_failures else "[FAIL]")
    + " SDK-native HTTP timeout: %d failure(s)" % http_timeout_test_failures
)
sys.exit(1 if http_timeout_test_failures else 0)
