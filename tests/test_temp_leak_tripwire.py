"""Offline proof that the session temp-leak classifier fails closed without flagging legitimate caches."""
import pytest

from conftest import assert_no_new_owned_temp_entries


OWNED_TEST_PREFIX = "fake-repo-"
LEGITIMATE_CACHE_TEST_PREFIX = "fastembed_cache"


def test_temp_leak_tripwire_rejects_owned_prefix(tmp_path):
    before = set()
    (tmp_path / f"{OWNED_TEST_PREFIX}leak").mkdir()
    with pytest.raises(pytest.fail.Exception):
        assert_no_new_owned_temp_entries(before, tmp_path)


def test_temp_leak_tripwire_allows_legitimate_cache_prefix(tmp_path):
    before = set()
    (tmp_path / f"{LEGITIMATE_CACHE_TEST_PREFIX}-owned-elsewhere").mkdir()
    assert_no_new_owned_temp_entries(before, tmp_path)


if __name__ == "__main__":
    from _pytest_fixture_bridge import run_pytest_style
    raise SystemExit(run_pytest_style(globals()))
