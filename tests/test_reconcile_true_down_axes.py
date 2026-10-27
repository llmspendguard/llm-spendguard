"""Offline mixed-axis and failed-fetch fault guard for staged batch correction."""
import os
import sys
import tempfile
import sqlite3
from contextlib import closing
from decimal import Decimal

if not os.environ.get("SPENDGUARD_TEST_ISOLATED"):
    os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"
    os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="spendguard-test-")
    os.execv(sys.executable, [sys.executable] + sys.argv)

from spendguard import backfill, budget, conv, ledger_sync, reconcile_anthropic, report, saas
from _script_checks import seed_test_spend_event

TRUE_DOWN_AXES_DAY = "2026-06-14"
TRUE_DOWN_AXES_SINCE = "2026-06-01"
TRUE_DOWN_AXES_PROVIDER = "anthropic"
TRUE_DOWN_AXES_FAILED_PROVIDER = "openai"
TRUE_DOWN_AXES_MODEL = "claude-opus-4-8"
TRUE_DOWN_AXES_OTHER_MODEL = "gpt-5.5"
TRUE_DOWN_AXES_PROJECT = "mixed-axis-fixture"
TRUE_DOWN_AXES_ESTIMATE = Decimal("100")
TRUE_DOWN_AXES_BILLED = Decimal("70")
TRUE_DOWN_AXES_OTHER_ESTIMATE = Decimal("50")
TRUE_DOWN_AXES_NONBATCH_AMOUNT = Decimal("2")
TRUE_DOWN_AXES_NONBATCH_KINDS = ("realtime", "meta", "subscription", "est_chat", "remote", "external")
TRUE_DOWN_AXES_SOURCE = "true-down-axes-guard"
TRUE_DOWN_AXES_BATCH_KIND = "batch"
TRUE_DOWN_AXES_UNUSED_TOKENS = 0
TRUE_DOWN_AXES_TIMESTAMP_SUFFIX = "T00:00:00+00:00"
TRUE_DOWN_AXES_SUMMARY_KEYS = {
    "since", "provider_total", "gate_attributed", "true_down", "ungoverned", "gap_rows",
    "gap_by_project", "coverage", "errors", "providers_ok", "complete", "unaccounted", "audit",
}

for true_down_axes_provider, true_down_axes_model, true_down_axes_amount in (
        (TRUE_DOWN_AXES_PROVIDER, TRUE_DOWN_AXES_MODEL, TRUE_DOWN_AXES_ESTIMATE),
        (TRUE_DOWN_AXES_FAILED_PROVIDER, TRUE_DOWN_AXES_OTHER_MODEL, TRUE_DOWN_AXES_OTHER_ESTIMATE)):
    seed_test_spend_event(TRUE_DOWN_AXES_DAY, true_down_axes_provider, true_down_axes_model,
        TRUE_DOWN_AXES_BATCH_KIND, true_down_axes_amount, TRUE_DOWN_AXES_PROJECT,
        timestamp_suffix=TRUE_DOWN_AXES_TIMESTAMP_SUFFIX, source=TRUE_DOWN_AXES_SOURCE,
        dedup_fields=("provider", "kind"), basis=budget.BASIS_ESTIMATE)
for true_down_axes_kind in TRUE_DOWN_AXES_NONBATCH_KINDS:
    seed_test_spend_event(TRUE_DOWN_AXES_DAY, TRUE_DOWN_AXES_PROVIDER, TRUE_DOWN_AXES_MODEL,
        true_down_axes_kind, TRUE_DOWN_AXES_NONBATCH_AMOUNT, TRUE_DOWN_AXES_PROJECT,
        timestamp_suffix=TRUE_DOWN_AXES_TIMESTAMP_SUFFIX, source=TRUE_DOWN_AXES_SOURCE,
        dedup_fields=("provider", "kind"))

from spendguard import config

with closing(config.open_ledger_connection()) as true_down_axes_connection:
    true_down_axes_connection.row_factory = sqlite3.Row
    true_down_axes_ledger = budget._ledger()
    true_down_axes_originals = {
        row["id"]: dict(row) for row in true_down_axes_connection.execute("SELECT * FROM spend_events")}
    true_down_axes_rows = [(TRUE_DOWN_AXES_PROVIDER, TRUE_DOWN_AXES_MODEL, float(TRUE_DOWN_AXES_BILLED),
        TRUE_DOWN_AXES_UNUSED_TOKENS, TRUE_DOWN_AXES_UNUSED_TOKENS,
        TRUE_DOWN_AXES_DAY, TRUE_DOWN_AXES_SOURCE)]
    true_down_axes_truth = {TRUE_DOWN_AXES_PROVIDER: true_down_axes_rows,
        TRUE_DOWN_AXES_FAILED_PROVIDER: []}
    # First settle both providers, then inject a sibling fetch failure. Prior failed
    # corrections must clear back to estimates, while the readable sibling rebuilds.
    ledger_sync.true_down(TRUE_DOWN_AXES_SINCE, true_down_axes_truth)
    true_down_axes_truth[TRUE_DOWN_AXES_FAILED_PROVIDER] = None
    true_down_axes_partial = ledger_sync.true_down(TRUE_DOWN_AXES_SINCE, true_down_axes_truth)
    true_down_axes_corrections = [dict(row) for row in true_down_axes_connection.execute(
        "SELECT * FROM spend_events WHERE conv_id=?", (budget._TRUE_DOWN_CONV,))]
    assert true_down_axes_corrections, "successful provider correction suppressed by nonbatch NULL amounts"
    assert all(row["provider"] == TRUE_DOWN_AXES_PROVIDER for row in true_down_axes_corrections)
    assert all(row["model"] == TRUE_DOWN_AXES_MODEL for row in true_down_axes_corrections)
    assert all(row["invoice_id"] == ":".join((TRUE_DOWN_AXES_PROVIDER, TRUE_DOWN_AXES_DAY[:7]))
        for row in true_down_axes_corrections)
    assert sum(Decimal(row["batch_usd"]) for row in true_down_axes_corrections) == (
        TRUE_DOWN_AXES_BILLED - TRUE_DOWN_AXES_ESTIMATE)
    assert true_down_axes_partial["skipped"] == [TRUE_DOWN_AXES_FAILED_PROVIDER]
    assert not true_down_axes_partial["estimate_unpriced_incomplete"]
    assert true_down_axes_originals == {
        row["id"]: dict(row) for row in true_down_axes_connection.execute(
            "SELECT * FROM spend_events WHERE conv_id IS NOT ?", (budget._TRUE_DOWN_CONV,))}

    report.openai_by_day = lambda: ({}, 0)
    reconcile_anthropic.cost_by_day = lambda since=None: ({TRUE_DOWN_AXES_DAY: TRUE_DOWN_AXES_BILLED}, {})
    backfill._openai_rows = lambda: []
    backfill._anthropic_rows = lambda: true_down_axes_rows
    conv.batch_project_map = lambda: {}
    saas.saas_connection = lambda: {"owns_account": True, "project": TRUE_DOWN_AXES_PROJECT}
    ledger_sync.audit_completeness = lambda: {"complete": True}
    true_down_axes_summary = ledger_sync.reconcile_into_ledger(TRUE_DOWN_AXES_SINCE)
    assert TRUE_DOWN_AXES_SUMMARY_KEYS <= true_down_axes_summary.keys()
    assert true_down_axes_summary["complete"]
    assert true_down_axes_summary["provider_total"] == TRUE_DOWN_AXES_BILLED
    assert true_down_axes_summary["gate_attributed"] == TRUE_DOWN_AXES_BILLED
    assert not true_down_axes_summary["gap_rows"]
    print("OK reconcile_true_down_axes: mixed axes preserve batch corrections, failed sibling isolation and summary contract")
