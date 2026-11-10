"""Provider keys resolve through the registry and declared SSOT, never an ambient repo .env."""
import contextlib
import io
import os
from pathlib import Path
import sys
import tempfile

from _pytest_fixture_bridge import align_test_startup_home

align_test_startup_home(prefix="sg-keys-single-source-")
os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"
os.environ.setdefault("SPENDGUARD_NO_AUTOINSTALL", "1")
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from spendguard.analyze.assurance import keys_audit  # noqa: E402
from spendguard.store.configuration import provider_keys  # noqa: E402
from spendguard.store.configuration.provider_key_env_registry import all_provider_key_env_items  # noqa: E402

SYNTHETIC_KEY_VALUE = "synthetic-key-value"
SSOT_KEY_VALUE = "ssot-key-value"


def keys_single_source_failures(scan_path):
    failures = []
    registry = all_provider_key_env_items()
    declared_keys = dict(provider_keys._iter_env_file(provider_keys.KEYS_ENV))
    for provider, key_env in sorted(registry.items()):
        if provider_keys._provider_key_env(provider) != key_env:
            failures.append(f"{provider}: registry resolution mismatch")
        os.environ.pop(key_env, None)
        resolved_key = provider_keys.api_key(key_env)
        if resolved_key and resolved_key != declared_keys.get(key_env):
            failures.append(f"{provider}: key resolved outside declared SSOT")
    with contextlib.redirect_stdout(io.StringIO()):
        scanner_exit = keys_audit.audit_provider_key_shadows([str(scan_path)], strict=True)
    if scanner_exit:
        failures.append(f"repo dotenv shadow scanner exit={scanner_exit}")
    return failures


def test_keys_single_source_rejects_synthetic_dotenv(tmp_path):
    provider_key_env = sorted(all_provider_key_env_items().values())[0]
    Path(provider_keys.KEYS_ENV).write_text(f"{provider_key_env}={SSOT_KEY_VALUE}\n")
    dotenv_path = tmp_path / ".env"
    dotenv_path.write_text(f"{provider_key_env}={SYNTHETIC_KEY_VALUE}\n")
    os.environ.pop(provider_key_env, None)
    previous_cwd = Path.cwd()
    try:
        os.chdir(tmp_path)
        assert provider_keys.api_key(provider_key_env) == SSOT_KEY_VALUE
        failures = keys_single_source_failures(tmp_path)
    finally:
        os.chdir(previous_cwd)
    assert any("scanner exit=" in failure for failure in failures)
    assert int(bool(failures)) == 1
    print("fail-proof keys-single-source injected repo .env key: exit 1")
    dotenv_path.unlink()
    assert keys_single_source_failures(tmp_path) == []


def keys_single_source_main():
    failures = keys_single_source_failures(Path.cwd())
    for failure in failures:
        print(f"FAIL {failure}")
    return int(bool(failures))


if __name__ == "__main__":
    with tempfile.TemporaryDirectory() as temporary_directory:
        test_keys_single_source_rejects_synthetic_dotenv(Path(temporary_directory))
    raise SystemExit(keys_single_source_main())
