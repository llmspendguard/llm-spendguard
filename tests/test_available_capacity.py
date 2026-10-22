"""Offline capacity admission proofs: cached reads and isolated flock slots only."""
import os
import tempfile
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

import spendguard
spendguard.require()
from spendguard import config, dispatch, lane_catalog, lanes

CAPACITY_TEST_CODEX_SLOTS = 20
CAPACITY_TEST_OTHER_SLOTS = dispatch.DEFAULT_LANE_CONCURRENCY
CAPACITY_TEST_LANES = ("codex", "capacity-test-a", "capacity-test-b")
CAPACITY_TEST_SUM = CAPACITY_TEST_CODEX_SLOTS + CAPACITY_TEST_OTHER_SLOTS * (len(CAPACITY_TEST_LANES) - 1)
CAPACITY_TEST_POOL = "capacity-proof"
CAPACITY_TEST_OVERRIDE = dispatch.DEFAULT_ADMIT - 1


def capacity_fixture_config(section, key, default=None):
    return default


with ExitStack() as capacity_stack:
    capacity_stack.enter_context(patch.dict(os.environ, {k: v for k, v in os.environ.items()
        if not k.startswith(dispatch._ENV_PREFIX)}, clear=True))
    capacity_stack.enter_context(patch.object(config, "_cfg_get", side_effect=capacity_fixture_config))
    capacity_stack.enter_context(patch.object(lane_catalog, "lanes", return_value=CAPACITY_TEST_LANES))
    capacity_stack.enter_context(patch.object(dispatch, "effective_codex_concurrency", return_value=CAPACITY_TEST_CODEX_SLOTS))
    capacity_probe = capacity_stack.enter_context(patch.object(lanes, "_last_probe_ok", return_value=(True, "cached")))
    capacity_stack.enter_context(patch.object(lanes, "probe", side_effect=AssertionError("must not spawn")))
    assert dispatch.available_capacity() == CAPACITY_TEST_SUM
    capacity_probe.return_value = (False, "")
    assert dispatch.available_capacity() == CAPACITY_TEST_SUM
    capacity_probe.return_value = None
    assert dispatch.available_capacity() == CAPACITY_TEST_SUM
    capacity_probe.return_value = (False, "cached")
    assert dispatch.available_capacity() == dispatch.DEFAULT_ADMIT
    capacity_probe.side_effect = RuntimeError("unreadable cache")
    assert dispatch.available_capacity() == dispatch.DEFAULT_ADMIT
    capacity_probe.side_effect = None
    capacity_probe.return_value = (True, "cached")
    with patch.object(lane_catalog, "lanes", side_effect=RuntimeError("unreadable catalog")):
        assert dispatch.available_capacity() == dispatch.DEFAULT_ADMIT
    with patch.object(lane_catalog, "lanes", return_value=()):
        assert dispatch.available_capacity() == dispatch.DEFAULT_ADMIT
    with patch.object(dispatch, "_configured_lane_capacity", return_value=0):
        assert dispatch.available_capacity() == dispatch.DEFAULT_ADMIT
    with patch.object(config, "_cfg_get", side_effect=RuntimeError("unreadable config")):
        assert dispatch.available_capacity() == dispatch.DEFAULT_ADMIT

    capacity_home = capacity_stack.enter_context(tempfile.TemporaryDirectory())
    capacity_stack.enter_context(patch.object(config, "HOME", Path(capacity_home)))
    capacity_stack.enter_context(patch.object(dispatch, "available_capacity", return_value=CAPACITY_TEST_SUM))
    for capacity_explicit in (None, CAPACITY_TEST_OVERRIDE):
        capacity_expected = CAPACITY_TEST_SUM if capacity_explicit is None else capacity_explicit
        capacity_handles = []
        try:
            for capacity_index in range(capacity_expected):
                capacity_handle = dispatch.try_admit(CAPACITY_TEST_POOL, limit=capacity_explicit)
                assert capacity_handle is not None, capacity_index
                capacity_handles.append(capacity_handle)
            assert dispatch.try_admit(CAPACITY_TEST_POOL, limit=capacity_explicit) is None
        finally:
            for capacity_handle in capacity_handles:
                capacity_handle.release()
    with patch.dict(os.environ, {"SPENDGUARD_DISPATCH_ADMIT_HOOKS": str(CAPACITY_TEST_OVERRIDE)}):
        assert dispatch._resolve_admit_ceiling("hooks") == CAPACITY_TEST_OVERRIDE
        capacity_handles = []
        try:
            for capacity_index in range(CAPACITY_TEST_OVERRIDE):
                capacity_handle = dispatch.try_admit("hooks")
                assert capacity_handle is not None
                capacity_handles.append(capacity_handle)
            assert dispatch.try_admit("hooks") is None
        finally:
            for capacity_handle in capacity_handles:
                capacity_handle.release()
    with patch.object(config, "_cfg_get", return_value=CAPACITY_TEST_OVERRIDE):
        assert dispatch._resolve_admit_ceiling("hooks") == CAPACITY_TEST_OVERRIDE
    for capacity_noop_switch in ("_off", "_xp_off"):
        with patch.object(dispatch, capacity_noop_switch, return_value=True):
            assert dispatch.try_admit(CAPACITY_TEST_POOL) is dispatch._NOOP_ADMIT

print("PASS available_capacity: cached readiness, floor, defensive fallback, capacity admission, overrides, noop")
