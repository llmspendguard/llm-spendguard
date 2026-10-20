"""Owned corruption rebuild, legacy metadata migration, and atomic fault guards."""
import os
import sys
import tempfile
import json
from pathlib import Path

if not os.environ.get("SPENDGUARD_TEST_ISOLATED"):
    os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"
    os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="spendguard-gap-ownership-")
    os.execv(sys.executable, [sys.executable] + sys.argv)

from spendguard import budget, ledger_sync, pricing
from spendguard.reconcile_gap_ownership import (
    migrate_reconcile_gap_provider_ownership, RECONCILE_GAP_UNKNOWN_REASON,
)
from _script_checks import seed_test_spend_event

GAP_OWNER_DAYS = ("2026-06-02", "2026-06-03", "2026-06-04", "2026-06-05")
GAP_OWNER_SINCE = "2026-06-01"
GAP_OWNER_PROJECT = "gap-ownership-fixture"
GAP_OWNER_MODEL = "gpt-5.5"
GAP_OWNER_PROVIDER = pricing.PROVIDERS[pricing.normalize(GAP_OWNER_MODEL)]
GAP_OWNER_OTHER_PROVIDER = "anthropic"
GAP_OWNER_OTHER_MODEL = "claude-opus-4-8"
GAP_OWNER_LEGACY = "(reconciled)"
GAP_OWNER_LEGACY_AMOUNT = 3.0
GAP_OWNER_BILLED_AMOUNT = 10.0
GAP_OWNER_CORRUPT_AMOUNT = "gap-ownership-corrupt-amount"
GAP_OWNER_SEED_UNOWNED_COUNT = 1
GAP_OWNER_SEED_BACKFILLED_COUNT = 2
GAP_OWNER_FAULT_MESSAGE = "injected ownership audit failure"
GAP_OWNER_INVOICE_PERIOD = "2026-06"
GAP_OWNER_EXCLUDED_DAY = "2026-05-31"

for gap_owner_day in GAP_OWNER_DAYS:
    budget.record_reconciled(gap_owner_day, GAP_OWNER_PROVIDER, GAP_OWNER_LEGACY_AMOUNT,
                             project=GAP_OWNER_PROJECT)
gap_owner_connection = budget._ledger()._conn
gap_owner_connection.execute("UPDATE spend_events SET provider=?", (GAP_OWNER_LEGACY,))
gap_owner_connection.execute("UPDATE spend_events SET invoice_id=NULL,model=? WHERE day=?",
                             (GAP_OWNER_MODEL, GAP_OWNER_DAYS[1]))
gap_owner_connection.execute("UPDATE spend_events SET invoice_id=NULL WHERE day IN (?,?)", GAP_OWNER_DAYS[2:])
gap_owner_connection.commit()
# The fourth row is the separate audit-fault fixture; hide its marker temporarily.
gap_owner_connection.execute("UPDATE spend_events SET recon_marker=NULL WHERE day=?", (GAP_OWNER_DAYS[3],))
gap_owner_connection.commit()
gap_owner_original_rows = [tuple(row) for row in gap_owner_connection.execute(
    "SELECT id,batch_usd,invoice_id,model,day FROM spend_events ORDER BY id")]
gap_owner_first = migrate_reconcile_gap_provider_ownership()
assert gap_owner_first["backfilled"] == GAP_OWNER_SEED_BACKFILLED_COUNT
assert gap_owner_first["unowned"] == GAP_OWNER_SEED_UNOWNED_COUNT
assert gap_owner_first["gaps"] == {RECONCILE_GAP_UNKNOWN_REASON: GAP_OWNER_SEED_UNOWNED_COUNT}
assert gap_owner_original_rows == [tuple(row) for row in gap_owner_connection.execute(
    "SELECT id,batch_usd,invoice_id,model,day FROM spend_events ORDER BY id")]
gap_owner_after_first = [tuple(row) for row in gap_owner_connection.execute("SELECT * FROM spend_events ORDER BY id")]
gap_owner_audits = gap_owner_connection.execute("SELECT COUNT(*) FROM spend_audit").fetchone()[0]
gap_owner_second = migrate_reconcile_gap_provider_ownership()
assert gap_owner_second["backfilled"] == 0
assert gap_owner_second["unowned"] == GAP_OWNER_SEED_UNOWNED_COUNT
assert gap_owner_after_first == [tuple(row) for row in gap_owner_connection.execute("SELECT * FROM spend_events ORDER BY id")]
assert gap_owner_audits == gap_owner_connection.execute("SELECT COUNT(*) FROM spend_audit").fetchone()[0]

# A mid-step audit failure must roll back ownership AND revision, not just money.
gap_owner_connection.execute("UPDATE spend_events SET recon_marker=?,invoice_id=? WHERE day=?",
    (budget._RECONCILED, GAP_OWNER_PROVIDER + ":" + GAP_OWNER_INVOICE_PERIOD, GAP_OWNER_DAYS[3]))
gap_owner_connection.commit()
gap_owner_before_fault = [tuple(row) for row in gap_owner_connection.execute("SELECT * FROM spend_events ORDER BY id")]
gap_owner_original_audit = budget._ledger()._audit


def reject_gap_ownership_audit_fault(*args, **kwargs):
    raise RuntimeError(GAP_OWNER_FAULT_MESSAGE)


budget._ledger()._audit = reject_gap_ownership_audit_fault
try:
    migrate_reconcile_gap_provider_ownership()
except RuntimeError as gap_owner_fault:
    assert str(gap_owner_fault) == GAP_OWNER_FAULT_MESSAGE
else:
    raise AssertionError("ownership migration ignored injected audit failure")
finally:
    budget._ledger()._audit = gap_owner_original_audit
assert gap_owner_before_fault == [tuple(row) for row in gap_owner_connection.execute("SELECT * FROM spend_events ORDER BY id")]
assert gap_owner_audits == gap_owner_connection.execute("SELECT COUNT(*) FROM spend_audit").fetchone()[0]
assert migrate_reconcile_gap_provider_ownership()["backfilled"] == GAP_OWNER_SEED_UNOWNED_COUNT

# Resolve the intentional unknown fixture from explicit provider evidence, then
# corrupt the other provider's raw estimate. Its owned settled gap must survive.
gap_owner_connection.execute("UPDATE spend_events SET provider=? WHERE day=?",
                             (GAP_OWNER_OTHER_PROVIDER, GAP_OWNER_DAYS[2]))
gap_owner_connection.commit()
seed_test_spend_event(GAP_OWNER_DAYS[0], GAP_OWNER_OTHER_PROVIDER, GAP_OWNER_OTHER_MODEL,
    "batch", GAP_OWNER_LEGACY_AMOUNT, GAP_OWNER_PROJECT, timestamp_suffix="T00:00:00+00:00",
    source="gap-ownership-corrupt-fixture", dedup_fields=("provider", "model"))
gap_owner_connection.execute("UPDATE spend_events SET batch_usd=? WHERE reconciled=0",
                             (GAP_OWNER_CORRUPT_AMOUNT,))
gap_owner_connection.commit()
from spendguard import report, reconcile_anthropic, backfill, conv, saas

report.openai_by_day = lambda: ({}, 0)
reconcile_anthropic.cost_by_day = lambda since=None: ({}, {})
backfill._openai_rows = lambda: [(GAP_OWNER_PROVIDER, GAP_OWNER_MODEL, GAP_OWNER_BILLED_AMOUNT,
    0, 0, GAP_OWNER_DAYS[0], GAP_OWNER_MODEL)]
backfill._anthropic_rows = lambda: [(GAP_OWNER_OTHER_PROVIDER, GAP_OWNER_OTHER_MODEL, GAP_OWNER_LEGACY_AMOUNT,
    0, 0, GAP_OWNER_DAYS[0], GAP_OWNER_OTHER_MODEL)]
conv.batch_project_map = lambda: {}
saas.saas_connection = lambda: {"owns_account": True, "project": GAP_OWNER_PROJECT}
ledger_sync.audit_completeness = lambda: {"complete": True}
gap_owner_preserved = [tuple(row) for row in gap_owner_connection.execute(
    "SELECT * FROM spend_events WHERE reconciled=1 AND provider=?", (GAP_OWNER_OTHER_PROVIDER,))]
gap_owner_rebuild = ledger_sync.reconcile_into_ledger(GAP_OWNER_SINCE)
assert "gap_rebuild_refused" not in gap_owner_rebuild
assert gap_owner_rebuild["errors"]["ledger_read"]
assert not gap_owner_rebuild["complete"]
assert gap_owner_preserved == [tuple(row) for row in gap_owner_connection.execute(
    "SELECT * FROM spend_events WHERE reconciled=1 AND provider=?", (GAP_OWNER_OTHER_PROVIDER,))]
gap_owner_forward_rows = list(gap_owner_connection.execute(
    "SELECT provider,invoice_id,batch_usd FROM spend_events WHERE reconciled=1 AND provider!=?",
    (GAP_OWNER_OTHER_PROVIDER,)))
assert len(gap_owner_forward_rows) == GAP_OWNER_SEED_UNOWNED_COUNT
assert gap_owner_forward_rows[0]["provider"] == GAP_OWNER_PROVIDER
assert gap_owner_forward_rows[0]["invoice_id"] == GAP_OWNER_PROVIDER + ":" + GAP_OWNER_INVOICE_PERIOD
assert float(gap_owner_forward_rows[0]["batch_usd"]) == GAP_OWNER_BILLED_AMOUNT

# A genuinely unowned settled gap still blocks partial rebuilding, fail closed.
budget.record_reconciled(GAP_OWNER_DAYS[1], GAP_OWNER_LEGACY, GAP_OWNER_LEGACY_AMOUNT,
                         project=GAP_OWNER_PROJECT)
gap_owner_before_refusal = [tuple(row) for row in gap_owner_connection.execute(
    "SELECT * FROM spend_events WHERE reconciled=1 ORDER BY id")]
gap_owner_refusal = ledger_sync.reconcile_into_ledger(GAP_OWNER_SINCE)
assert gap_owner_refusal["gap_rebuild_refused"]
assert gap_owner_refusal["ownership_migration"]["unowned"] == GAP_OWNER_SEED_UNOWNED_COUNT
assert gap_owner_before_refusal == [tuple(row) for row in gap_owner_connection.execute(
    "SELECT * FROM spend_events WHERE reconciled=1 ORDER BY id")]
gap_owner_connection.execute("UPDATE spend_events SET batch_usd=? WHERE reconciled=0",
                             (GAP_OWNER_LEGACY_AMOUNT,))
gap_owner_connection.commit()
gap_owner_original_billed_fetch = backfill._anthropic_rows


def raise_gap_ownership_billed_fetch_fault():
    raise RuntimeError(GAP_OWNER_FAULT_MESSAGE)


backfill._anthropic_rows = raise_gap_ownership_billed_fetch_fault
try:
    gap_owner_fetch_refusal = ledger_sync.reconcile_into_ledger(GAP_OWNER_SINCE)
finally:
    backfill._anthropic_rows = gap_owner_original_billed_fetch
assert gap_owner_fetch_refusal["gap_rebuild_refused"]
assert gap_owner_before_refusal == [tuple(row) for row in gap_owner_connection.execute(
    "SELECT * FROM spend_events WHERE reconciled=1 ORDER BY id")]

# Complete truth can safely replace every old gap, including a legacy unknown.
gap_owner_healthy_rebuild = ledger_sync.reconcile_into_ledger(GAP_OWNER_SINCE)
assert "gap_rebuild_refused" not in gap_owner_healthy_rebuild
assert gap_owner_healthy_rebuild["complete"]
gap_owner_healthy_rows = list(gap_owner_connection.execute(
    "SELECT provider,batch_usd FROM spend_events WHERE reconciled=1"))
assert len(gap_owner_healthy_rows) == GAP_OWNER_SEED_UNOWNED_COUNT
assert all(row["provider"] == GAP_OWNER_PROVIDER for row in gap_owner_healthy_rows)
assert sum(float(row["batch_usd"]) for row in gap_owner_healthy_rows) == GAP_OWNER_BILLED_AMOUNT
gap_owner_current_billed_rows = backfill._openai_rows()
backfill._openai_rows = lambda: gap_owner_current_billed_rows + [(
    GAP_OWNER_PROVIDER, GAP_OWNER_MODEL, GAP_OWNER_LEGACY_AMOUNT, 0, 0,
    GAP_OWNER_EXCLUDED_DAY, GAP_OWNER_PROJECT)]
assert ledger_sync.reconcile_into_ledger(GAP_OWNER_SINCE)["excluded_billed_rows"] == {
    "out_of_window": GAP_OWNER_SEED_UNOWNED_COUNT}
from spendguard import config

gap_owner_log_rows = [
    {"day": GAP_OWNER_EXCLUDED_DAY, "provider": GAP_OWNER_PROVIDER, "cost": GAP_OWNER_LEGACY_AMOUNT},
    {"provider": GAP_OWNER_PROVIDER, "cost": GAP_OWNER_LEGACY_AMOUNT},
]
Path(config.RT_LOG).write_text("".join(json.dumps(row) + "\n" for row in gap_owner_log_rows))
ledger_sync.record_realtime_reconstruction = lambda: None
gap_owner_excluded_counts = {"out_of_window": GAP_OWNER_SEED_UNOWNED_COUNT,
                           "missing_day": GAP_OWNER_SEED_UNOWNED_COUNT}
assert ledger_sync.reconcile_realtime(GAP_OWNER_SINCE)["excluded_log_rows"] == gap_owner_excluded_counts
assert ledger_sync.realtime_check(0, GAP_OWNER_SINCE)["excluded_log_rows"] == gap_owner_excluded_counts
print("OK reconcile_gap_ownership: forward ownership, two legacy derivations, one named unknown, idempotence, atomic audit fault, corrupt owned rebuild, partial unknown refusal, healthy replacement")
