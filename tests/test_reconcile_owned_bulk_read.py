"""Offline reconcile streams retain whole-read deadlines without request dispatch."""
from contextlib import closing, contextmanager
import time
import sys
import unittest
from unittest.mock import patch

from _pytest_fixture_bridge import align_test_startup_home

align_test_startup_home(prefix=__name__)

from spendguard import budget, config, ledger_read, ledger_request_cursor, reconcile_estimate_stage
from _script_checks import seed_test_spend_event

RECONCILE_OWNED_GUARD_DAY = "2026-06-03"
RECONCILE_OWNED_GUARD_PROVIDER = "openai"
RECONCILE_OWNED_GUARD_MODEL = "gpt-5.5"
RECONCILE_OWNED_GUARD_AMOUNT = 2
RECONCILE_OWNED_GUARD_DEADLINE_S = 0.02
RECONCILE_OWNED_GUARD_DELAY_MULTIPLIER = 2
RECONCILE_OWNED_GUARD_SOURCE_SCANS = 2
RECONCILE_OWNED_GUARD_PROFILE_EVENT = "call"
RECONCILE_OWNED_GUARD_CALLBACK_NAME = "ledger_read_progress"


class ReconcileOwnedBulkReadGuards(unittest.TestCase):
    def test_reconcile_native_streams_require_owned_deadline_and_reject_old_owner_mutation(self):
        seed_test_spend_event(RECONCILE_OWNED_GUARD_DAY, RECONCILE_OWNED_GUARD_PROVIDER,
            RECONCILE_OWNED_GUARD_MODEL, "batch", RECONCILE_OWNED_GUARD_AMOUNT, __name__,
            timestamp_suffix="T00:00:00+00:00", source=__name__, dedup_fields=("project",))
        original_owner = ledger_read._ledger_read_connection
        original_dispatch = ledger_request_cursor.ManagedRequestCursorState.dispatch_managed_request_cursor_step
        observed_owners = []
        callback_count = 0

        def count_reconcile_owned_source_callbacks(frame, event, argument):
            nonlocal callback_count
            if (event == RECONCILE_OWNED_GUARD_PROFILE_EVENT
                    and frame.f_code.co_name == RECONCILE_OWNED_GUARD_CALLBACK_NAME):
                callback_count += 1

        @contextmanager
        def observe_reconcile_owned_bulk_handle(*args, **kwargs):
            assert kwargs.get("bulk_chunked") is True, "offline source must poll at chunk boundaries"
            with original_owner(*args, **kwargs) as source:
                observed_owners.append(source)
                assert source._ledger_owned_read_deadline
                previous_profiler = sys.getprofile()
                sys.setprofile(count_reconcile_owned_source_callbacks)
                try:
                    yield source
                finally:
                    sys.setprofile(previous_profiler)

        def refuse_reconcile_owned_cursor_dispatch(state, operation, *args, **kwargs):
            connection = state._request_cursor_referent.connection
            assert not getattr(connection, "_ledger_owned_read_deadline", False), (
                "offline source must use native cursor, not per-row managed dispatch")
            return original_dispatch(state, operation, *args, **kwargs)

        with patch.object(reconcile_estimate_stage, "_ledger_read_connection", observe_reconcile_owned_bulk_handle,
                          create=True), \
                patch.object(ledger_request_cursor.ManagedRequestCursorState,
                    "dispatch_managed_request_cursor_step", refuse_reconcile_owned_cursor_dispatch):
            with reconcile_estimate_stage.stage_reconcile_estimate_cells(
                    RECONCILE_OWNED_GUARD_DAY, budget._TRUE_DOWN_CONV, str) as (staged, gaps):
                self.assertFalse(gaps)
                self.assertEqual(staged.execute("SELECT COUNT(*) FROM cells").fetchone()[0], 1)
            self.assertEqual(dict(reconcile_estimate_stage.iter_reconcile_gate_project_days(
                RECONCILE_OWNED_GUARD_DAY)), {(__name__, RECONCILE_OWNED_GUARD_DAY): RECONCILE_OWNED_GUARD_AMOUNT})
        self.assertEqual(len(observed_owners), RECONCILE_OWNED_GUARD_SOURCE_SCANS)
        self.assertEqual(callback_count, 0)
        print(f"actual reconcile staging and gap scan VM callbacks: {callback_count}")

        @contextmanager
        def legacy_reconcile_unbounded_owner_mutation(path, time_budget, *, bulk_chunked=False):
            # Reintroduce the old managed connection without an owned deadline.
            with closing(config.open_ledger_connection(path)) as source:
                source._ledger_bulk_chunk_live = lambda: None
                yield source

        def expire_reconcile_staging_work(model):
            time.sleep(RECONCILE_OWNED_GUARD_DEADLINE_S * RECONCILE_OWNED_GUARD_DELAY_MULTIPLIER)
            return model

        def require_reconcile_whole_scan_timeout():
            with self.assertRaises(ledger_read.LedgerReadTimeout):
                with reconcile_estimate_stage.stage_reconcile_estimate_cells(
                        RECONCILE_OWNED_GUARD_DAY, budget._TRUE_DOWN_CONV, expire_reconcile_staging_work,
                        time_budget=RECONCILE_OWNED_GUARD_DEADLINE_S):
                    self.fail("expired offline scan exposed a completed stage")

        require_reconcile_whole_scan_timeout()
        with patch.object(reconcile_estimate_stage, "_ledger_read_connection", legacy_reconcile_unbounded_owner_mutation):
            with self.assertRaises(AssertionError):
                require_reconcile_whole_scan_timeout()


if __name__ == "__main__":
    unittest.main()
