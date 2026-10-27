"""Offline correction writers remain atomic under interruption and concurrent rebuilds."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from decimal import Decimal
import unittest
from unittest.mock import patch

from _pytest_fixture_bridge import align_test_startup_home

align_test_startup_home(prefix=__name__)

from spendguard import budget, config, ledger_request_cursor, ledger_sync, pricing, reconcile_maintenance_owner
from spendguard.ledger import LEDGER_RATE_PROVENANCE_GAP
from _script_checks import seed_test_spend_event

RECONCILE_ATOMIC_GUARD_DAY = "2026-06-03"
RECONCILE_ATOMIC_GUARD_PROVIDER = "openai"
RECONCILE_ATOMIC_GUARD_MODEL = "gpt-5.5"
RECONCILE_ATOMIC_GUARD_ESTIMATE = 2
RECONCILE_ATOMIC_GUARD_BILLED = 1
RECONCILE_ATOMIC_GUARD_CALLERS = 2
RECONCILE_ATOMIC_GUARD_LOGGER = "spendguard.ledger"
RECONCILE_ATOMIC_GUARD_LOG_LEVEL = "WARNING"


class ReconcileAtomicMaintenanceGuards(unittest.TestCase):
    def test_reconcile_correction_interrupt_and_concurrency_preserve_exact_audit(self):
        seed_test_spend_event(RECONCILE_ATOMIC_GUARD_DAY, RECONCILE_ATOMIC_GUARD_PROVIDER,
            RECONCILE_ATOMIC_GUARD_MODEL, "batch", RECONCILE_ATOMIC_GUARD_ESTIMATE, __name__,
            timestamp_suffix="T00:00:00+00:00", source=__name__, dedup_fields=("project",))
        billed = {RECONCILE_ATOMIC_GUARD_PROVIDER: [
            (RECONCILE_ATOMIC_GUARD_PROVIDER, RECONCILE_ATOMIC_GUARD_MODEL,
             RECONCILE_ATOMIC_GUARD_BILLED, 0, 0, RECONCILE_ATOMIC_GUARD_DAY, __name__)]}
        owner = reconcile_maintenance_owner.owned_reconcile_maintenance_connection

        def reject_reconcile_maintenance_managed_dispatch(*args, **kwargs):
            raise AssertionError("offline correction used managed request dispatch")

        @contextmanager
        def observe_reconcile_atomic_native_owner(*args, **kwargs):
            with owner(*args, **kwargs) as connection:
                with patch.object(ledger_request_cursor.ManagedRequestCursorState,
                        "dispatch_managed_request_cursor_step", reject_reconcile_maintenance_managed_dispatch):
                    yield connection

        def run_reconcile_atomic_guard_rebuild():
            return ledger_sync.true_down(RECONCILE_ATOMIC_GUARD_DAY, billed_rows=billed)

        def capture_reconcile_atomic_guard_rows():
            with config.managed_ledger_read_connection(config.db_path()) as connection:
                return tuple(tuple(row) for row in connection.execute("SELECT * FROM spend_events ORDER BY id")), \
                    tuple(tuple(row) for row in connection.execute("SELECT * FROM spend_audit ORDER BY rowid"))

        with patch.object(reconcile_maintenance_owner, "owned_reconcile_maintenance_connection",
                          observe_reconcile_atomic_native_owner):
            run_reconcile_atomic_guard_rebuild()
            before = capture_reconcile_atomic_guard_rows()
            original_record = budget.record_true_down

            def interrupt_reconcile_atomic_guard_after_insert(*args, **kwargs):
                original_record(*args, **kwargs)
                raise KeyboardInterrupt("injected interrupt after correction insert")

            with patch.object(budget, "record_true_down", interrupt_reconcile_atomic_guard_after_insert):
                with self.assertRaises(KeyboardInterrupt):
                    run_reconcile_atomic_guard_rebuild()
            self.assertEqual(capture_reconcile_atomic_guard_rows(), before)
            with ThreadPoolExecutor(max_workers=RECONCILE_ATOMIC_GUARD_CALLERS) as callers:
                results = list(callers.map(lambda _: run_reconcile_atomic_guard_rebuild(),
                                          range(RECONCILE_ATOMIC_GUARD_CALLERS)))
            self.assertTrue(all(result["trued_down"] == RECONCILE_ATOMIC_GUARD_BILLED for result in results))
        ledger = budget._ledger()
        with config.managed_ledger_read_connection(config.db_path()) as connection:
            corrections = connection.execute("SELECT batch_usd FROM spend_events WHERE conv_id=?",
                                              (budget._TRUE_DOWN_CONV,)).fetchall()
        self.assertEqual([Decimal(row[0]) for row in corrections], [Decimal(-RECONCILE_ATOMIC_GUARD_BILLED)])
        self.assertTrue(ledger.verify_audit_chain()[0])
        with patch.object(pricing, "price", side_effect=pricing.PriceUnknownError(__name__)), \
                patch.object(pricing, "_catalog_price", return_value=None), \
                patch.dict(pricing.PRICING, {}, clear=True):
            with self.assertLogs(RECONCILE_ATOMIC_GUARD_LOGGER, level=RECONCILE_ATOMIC_GUARD_LOG_LEVEL) as gap:
                evidence_id = ledger.record_event(dict(model=RECONCILE_ATOMIC_GUARD_MODEL,
                    batch_usd=RECONCILE_ATOMIC_GUARD_ESTIMATE, source=__name__))
        self.assertTrue(any(LEDGER_RATE_PROVENANCE_GAP in message for message in gap.output))
        with config.managed_ledger_read_connection(config.db_path()) as connection:
            evidence = connection.execute(
                "SELECT batch_usd, rate_in, rate_out FROM spend_events WHERE id=?", (evidence_id,)).fetchone()
        self.assertEqual(tuple(evidence), (str(RECONCILE_ATOMIC_GUARD_ESTIMATE), None, None))
        self.assertTrue(ledger.verify_audit_chain()[0])


if __name__ == "__main__":
    unittest.main()
