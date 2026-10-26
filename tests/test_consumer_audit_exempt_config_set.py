"""The consumer-audit exemption has a validated, top-level config write path.

Offline: isolated SPENDGUARD_HOME, no network, no model calls.
"""

import json
import os
from pathlib import Path
import sys
import tempfile

if not os.environ.get("SPENDGUARD_TEST_ISOLATED"):
    os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"
    os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="spendguard-consumer-exempt-")
    os.execv(sys.executable, [sys.executable] + sys.argv)

from spendguard import config, config_schema, setup


def check_consumer_exempt_config(consumer_exempt_failures, label, condition):
    passed = bool(condition)
    if not passed:
        consumer_exempt_failures.append(label)
    print(f"  [{'OK' if passed else 'FAIL'}] {label}")


def consumer_exempt_config_contents():
    return json.loads(config.CONFIG_JSON.read_text()) if config.CONFIG_JSON.exists() else {}


def main():
    consumer_exempt_failures = []
    consumer_exempt_setting = next(
        setting
        for setting in config_schema.SETTINGS
        if setting["section"] == "consumers" and setting["key"] == "audit_exempt"
    )
    consumer_exempt_paths = [str(config.HOME / "x_abs"), str(config.HOME / "y_abs")]

    print("-- top-level consumer exemption round-trips through the sanctioned config command --")
    check_consumer_exempt_config(
        consumer_exempt_failures,
        "before set, consumer audit uses the empty default",
        config.consumer_audit_settings()["consumer_audit_exempt"] == (),
    )
    check_consumer_exempt_config(
        consumer_exempt_failures,
        "set succeeds",
        setup.cmd_config(["set", "consumers.audit_exempt", json.dumps(consumer_exempt_paths)]) == 0,
    )
    consumer_exempt_stored = consumer_exempt_config_contents()
    check_consumer_exempt_config(
        consumer_exempt_failures,
        "the exemption is a top-level config.json key",
        consumer_exempt_stored.get("consumer_audit_exempt") == consumer_exempt_paths,
    )
    check_consumer_exempt_config(
        consumer_exempt_failures,
        "the write did not create a consumers section",
        "consumers" not in consumer_exempt_stored,
    )
    check_consumer_exempt_config(
        consumer_exempt_failures,
        "consumer audit resolves every configured path",
        config.consumer_audit_settings()["consumer_audit_exempt"]
        == tuple(Path(value).resolve() for value in consumer_exempt_paths),
    )
    check_consumer_exempt_config(
        consumer_exempt_failures,
        "the schema resolver reports the stored list and source",
        setup._resolve_setting(consumer_exempt_setting) == (consumer_exempt_paths, "config.json"),
    )

    print("-- null unsets, invalid shapes never persist, and nested stores remain nested --")
    check_consumer_exempt_config(
        consumer_exempt_failures,
        "null unsets the exemption",
        setup.cmd_config(["set", "consumers.audit_exempt", "null"]) == 0,
    )
    check_consumer_exempt_config(
        consumer_exempt_failures,
        "unset removes the top-level key",
        "consumer_audit_exempt" not in consumer_exempt_config_contents(),
    )
    check_consumer_exempt_config(
        consumer_exempt_failures,
        "unset restores the consumer-audit default",
        config.consumer_audit_settings()["consumer_audit_exempt"] == (),
    )
    for consumer_exempt_invalid_value in ('"not-a-list"', '{"a": 1}', '["valid", 1]'):
        consumer_exempt_before_rejection = consumer_exempt_config_contents()
        check_consumer_exempt_config(
            consumer_exempt_failures,
            f"invalid shape is rejected: {consumer_exempt_invalid_value}",
            setup.cmd_config(["set", "consumers.audit_exempt", consumer_exempt_invalid_value]) != 0,
        )
        check_consumer_exempt_config(
            consumer_exempt_failures,
            "a rejected shape leaves config.json unchanged",
            consumer_exempt_config_contents() == consumer_exempt_before_rejection,
        )
    check_consumer_exempt_config(
        consumer_exempt_failures,
        "an existing nested setting still writes successfully",
        setup.cmd_config(["set", "caps.per_batch", "30"]) == 0,
    )
    check_consumer_exempt_config(
        consumer_exempt_failures,
        "the existing nested store shape is unchanged",
        consumer_exempt_config_contents() == {"caps": {"per_batch": 30.0}},
    )

    print(
        f"\n{'[FAIL]' if consumer_exempt_failures else 'OK'} test_consumer_audit_exempt_config_set: "
        f"{len(consumer_exempt_failures)} failure(s)"
    )
    return 1 if consumer_exempt_failures else 0


sys.exit(main())
