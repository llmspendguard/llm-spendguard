"""Pending ledger totals never escape a transaction through a display memo."""
import sys
from contextlib import nullcontext
from unittest.mock import patch

import pytest
from _pytest_fixture_bridge import align_test_startup_home, run_pytest_style

align_test_startup_home(prefix=__name__)

from spendguard import ledger, ledger_writer, ledger_sync, receipt
from spendguard.ledger_read import ledger_request_scope

PENDING_TOTALS_WATCHDOG_S = 10
PENDING_TOTALS_AMOUNT = 1
PENDING_REPORT_MARKER = __name__


@pytest.mark.parametrize("owned", [False, True])
@pytest.mark.parametrize("inside_writer", [False, True])
def test_pending_total_memo_cannot_survive_bulk_rollback(owned, inside_writer):
    scope = (ledger_writer.exclusive_ledger_writer_connection() if owned else nullcontext(None))
    with ledger_request_scope(deadline_s=PENDING_TOTALS_WATCHDOG_S), scope as connection:
        current = ledger.SpendLedger(owned_connection=connection)
        committed = ledger.to_dec(current.spent_dec())
        assert ledger.to_dec(current.running_llm_total_dec()) == committed

        def abort_pending_totals_scope():
            with pytest.raises(RuntimeError):
                with current.bulk():
                    current.record_event({"source": __name__, "kind": "realtime", "usd": PENDING_TOTALS_AMOUNT,
                        "dedup_key": f"{__name__}:{owned}:{inside_writer}"})
                    assert ledger.to_dec(current.running_llm_total_dec()) == committed + PENDING_TOTALS_AMOUNT
                    raise RuntimeError(__name__)

        if inside_writer and not owned:
            ledger_writer.run_ledger_write(abort_pending_totals_scope)
        else:
            abort_pending_totals_scope()
        assert ledger.to_dec(current.spent_dec()) == committed
        assert ledger.to_dec(current.sum_dec()) == committed
        assert ledger.to_dec(current.running_llm_total_dec()) == committed
        assert ledger.to_dec(current.remote_dec()) == 0
        assert ledger.to_dec(current.subscription_dec()) == 0
        assert ledger.to_dec(current.est_value_dec()) == 0


def test_pending_coverage_and_tally_never_persist_after_rollback(tmp_path):
    coverage_cache = tmp_path / "coverage"
    tally_cache = tmp_path / "tally"
    tally_cache.write_text(PENDING_REPORT_MARKER)
    current = ledger.SpendLedger()
    with ledger_request_scope(deadline_s=PENDING_TOTALS_WATCHDOG_S), \
            patch.object(ledger_sync, "_leak_cache_path", return_value=coverage_cache), \
            patch.object(ledger_sync, "_compute", return_value={}), \
            patch.object(ledger_sync, "_render_leak_line", return_value=PENDING_REPORT_MARKER), \
            patch.object(receipt, "_statusline_cache_path", return_value=tally_cache), \
            patch.object(receipt, "tally", return_value={}), \
            patch.object(receipt, "render_line", return_value=""), \
            patch("spendguard.compaction.measured_explicit_compaction_ratio", return_value=(None, 0)), \
            patch("spendguard.claudecode.measured_compaction_ratio", return_value=(PENDING_TOTALS_AMOUNT, 1)), \
            patch.object(receipt.config, "update_json") as publish_report, \
            patch("spendguard.lane_value.refresh_lane_value_if_stale"):
        with pytest.raises(RuntimeError):
            with current.bulk():
                current.record_event({"source": __name__, "kind": "realtime", "usd": PENDING_TOTALS_AMOUNT,
                    "dedup_key": test_pending_coverage_and_tally_never_persist_after_rollback.__name__})
                assert ledger_sync.leak_line() == PENDING_REPORT_MARKER
                assert receipt._cached_tally_line() == ""
                receipt._write_statusline_cache("")
                assert receipt._compaction_hint()["k"] == PENDING_TOTALS_AMOUNT
                assert not publish_report.called
                raise RuntimeError(__name__)
    assert not coverage_cache.exists()
    assert tally_cache.read_text() == PENDING_REPORT_MARKER


if __name__ == "__main__":
    sys.exit(run_pytest_style(globals()))
