"""Full-ledger reconciliation and late malformed-row containment, entirely offline."""
import os
import sys
import tempfile
from decimal import Decimal

if not os.environ.get("SPENDGUARD_TEST_ISOLATED"):
    os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"
    os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="spendguard-test-")
    os.execv(sys.executable, [sys.executable] + sys.argv)

from spendguard import budget, ledger_sync
from spendguard.ledger_read import DEFAULT_LEDGER_READ_ROW_CAP
from _script_checks import seed_test_spend_event

RECONCILE_GUARD_EXTRA_ROWS = 7
RECONCILE_GUARD_DAY = "2026-06-03"
RECONCILE_GUARD_OTHER_DAY = "2026-06-04"
RECONCILE_GUARD_SINCE = "2026-06-01"
RECONCILE_GUARD_AMOUNT = Decimal("2")
RECONCILE_GUARD_PROVIDERS = ("openai", "anthropic")
RECONCILE_GUARD_MODEL = "gpt-5.5"
RECONCILE_GUARD_BAD_AMOUNT = "unreadable-estimate-amount"
RECONCILE_GUARD_BILLED_FRACTION = Decimal("0.5")
RECONCILE_GUARD_GAP = Decimal("10")
RECONCILE_GUARD_GAP_MODEL = "gpt-5.5-mini"

seed_test_spend_event(RECONCILE_GUARD_DAY, RECONCILE_GUARD_PROVIDERS[0], RECONCILE_GUARD_MODEL,
                      "batch", RECONCILE_GUARD_AMOUNT, "seed-project", timestamp_suffix="T00:00:00+00:00",
                      source="reconcile-containment-fixture", dedup_fields=("project",))
reconcile_guard_connection = budget._ledger()._conn
reconcile_guard_template = dict(reconcile_guard_connection.execute("SELECT * FROM spend_events").fetchone())
reconcile_guard_connection.execute("DELETE FROM spend_events")
reconcile_guard_columns = tuple(reconcile_guard_template)
reconcile_guard_insert = ("INSERT INTO spend_events (" + ",".join(reconcile_guard_columns) + ") VALUES (" +
                          ",".join("?" for _ in reconcile_guard_columns) + ")")
reconcile_guard_reference = {}
for reconcile_guard_index in range((DEFAULT_LEDGER_READ_ROW_CAP + RECONCILE_GUARD_EXTRA_ROWS)
                                    * len(RECONCILE_GUARD_PROVIDERS)):
    reconcile_guard_row = dict(reconcile_guard_template)
    reconcile_guard_provider = RECONCILE_GUARD_PROVIDERS[reconcile_guard_index % len(RECONCILE_GUARD_PROVIDERS)]
    reconcile_guard_day = (RECONCILE_GUARD_DAY if reconcile_guard_index % len(RECONCILE_GUARD_PROVIDERS)
                           else RECONCILE_GUARD_OTHER_DAY)
    reconcile_guard_project = f"reconcile-project-{reconcile_guard_index}"
    reconcile_guard_row.update(id=str(reconcile_guard_index), dedup_key=str(reconcile_guard_index),
                               provider=reconcile_guard_provider, day=reconcile_guard_day,
                               project_primary=reconcile_guard_project)
    reconcile_guard_connection.execute(reconcile_guard_insert,
                                       tuple(reconcile_guard_row[c] for c in reconcile_guard_columns))
    reconcile_guard_reference[(reconcile_guard_project, reconcile_guard_provider, RECONCILE_GUARD_MODEL,
                               reconcile_guard_day)] = float(RECONCILE_GUARD_AMOUNT * RECONCILE_GUARD_BILLED_FRACTION)
reconcile_guard_connection.commit()
reconcile_guard_reference = {
    (reconcile_reference_row["project_primary"], reconcile_reference_row["provider"],
     reconcile_reference_row["model"], reconcile_reference_row["day"]):
        float(Decimal(str(reconcile_reference_row["batch_usd"])) * RECONCILE_GUARD_BILLED_FRACTION)
    for reconcile_reference_row in reconcile_guard_connection.execute("SELECT * FROM spend_events")}
reconcile_guard_original_delete = budget._ledger().delete


def reconcile_clear_admission_delete(*args, **kwargs):
    """A clear must not hand delete's fetchall more than the ordinary row cap."""
    reconcile_clear_where, reconcile_clear_parameters = budget._ledger()._where(
        kwargs.get("since"), kwargs.get("until"), kwargs.get("where"))
    if kwargs.get("filt"):
        reconcile_clear_where += " AND " + kwargs["filt"]
    reconcile_clear_matches = reconcile_guard_connection.execute(
        "SELECT COUNT(*) FROM spend_events WHERE 1=1" + reconcile_clear_where,
        reconcile_clear_parameters).fetchone()[0]
    assert reconcile_clear_matches <= DEFAULT_LEDGER_READ_ROW_CAP
    return reconcile_guard_original_delete(*args, **kwargs)


budget._ledger().delete = reconcile_clear_admission_delete
reconcile_guard_billed = {}
for reconcile_guard_provider in RECONCILE_GUARD_PROVIDERS:
    reconcile_guard_total = sum(Decimal(str(v)) for k, v in reconcile_guard_reference.items()
                                if k[1] == reconcile_guard_provider)
    reconcile_guard_billed[reconcile_guard_provider] = [(reconcile_guard_provider, RECONCILE_GUARD_MODEL,
        float(reconcile_guard_total), 0, 0, RECONCILE_GUARD_DAY, reconcile_guard_provider)]

reconcile_guard_summary = ledger_sync.true_down(RECONCILE_GUARD_SINCE, reconcile_guard_billed)
reconcile_guard_actual = {(r["project_primary"], r["provider"], r["model"], r["day"]): -float(r["batch_usd"])
    for r in reconcile_guard_connection.execute("SELECT * FROM spend_events WHERE conv_id=?", (budget._TRUE_DOWN_CONV,))}
assert reconcile_guard_actual == reconcile_guard_reference
assert not reconcile_guard_summary["read_gaps"]

# Exercise the actual gap rebuild too: provider truth exceeds every readable
# gate cell by one known billed batch, with all billed usage on the same day.
from spendguard import report, reconcile_anthropic, backfill, conv, saas

reconcile_guard_billed[RECONCILE_GUARD_PROVIDERS[0]].append((RECONCILE_GUARD_PROVIDERS[0],
    RECONCILE_GUARD_GAP_MODEL, float(RECONCILE_GUARD_GAP), 0, 0, RECONCILE_GUARD_DAY, RECONCILE_GUARD_GAP_MODEL))
report.openai_by_day = lambda: ({}, 0)
reconcile_anthropic.cost_by_day = lambda since=None: ({}, {})
backfill._openai_rows = lambda: reconcile_guard_billed[RECONCILE_GUARD_PROVIDERS[0]]
backfill._anthropic_rows = lambda: reconcile_guard_billed[RECONCILE_GUARD_PROVIDERS[1]]
conv.batch_project_map = lambda: {}
saas.saas_connection = lambda: {"owns_account": True, "project": "unattributed"}
ledger_sync.audit_completeness = lambda: {"complete": True}
reconcile_guard_complete = ledger_sync.reconcile_into_ledger(RECONCILE_GUARD_SINCE)
assert reconcile_guard_complete["complete"]
reconcile_guard_gap_rows = [(r["day"], r["project_primary"], Decimal(str(r["batch_usd"])))
    for r in reconcile_guard_connection.execute("SELECT * FROM spend_events WHERE reconciled=1")]
assert reconcile_guard_gap_rows == [(RECONCILE_GUARD_DAY, "unattributed", RECONCILE_GUARD_GAP)]

# Corrupt a raw row beyond the old admission cap. Preserve this provider's settled
# corrections while the other provider rebuilds from all its readable estimates.
reconcile_guard_late_id = str(DEFAULT_LEDGER_READ_ROW_CAP + RECONCILE_GUARD_EXTRA_ROWS - 1)
reconcile_guard_failed_provider = reconcile_guard_connection.execute(
    "SELECT provider FROM spend_events WHERE id=?", (reconcile_guard_late_id,)).fetchone()[0]
reconcile_guard_settled_ids = {r[0] for r in reconcile_guard_connection.execute(
    "SELECT id FROM spend_events WHERE conv_id=? AND provider=?", (budget._TRUE_DOWN_CONV, reconcile_guard_failed_provider))}
reconcile_guard_connection.execute("UPDATE spend_events SET batch_usd=? WHERE id=?",
                                    (RECONCILE_GUARD_BAD_AMOUNT, reconcile_guard_late_id))
reconcile_guard_connection.commit()
reconcile_guard_chaos = ledger_sync.true_down(RECONCILE_GUARD_SINCE, reconcile_guard_billed)
assert reconcile_guard_late_id in str(reconcile_guard_chaos["read_gaps"])
assert "Traceback (most recent call last)" in str(reconcile_guard_chaos["read_gaps"])
assert "InvalidOperation" in str(reconcile_guard_chaos["read_gaps"])
assert reconcile_guard_settled_ids == {r[0] for r in reconcile_guard_connection.execute(
    "SELECT id FROM spend_events WHERE conv_id=? AND provider=?", (budget._TRUE_DOWN_CONV, reconcile_guard_failed_provider))}
reconcile_guard_readable_actual = {(r["project_primary"], r["provider"], r["model"], r["day"]): -float(r["batch_usd"])
    for r in reconcile_guard_connection.execute("SELECT * FROM spend_events WHERE conv_id=? AND provider!=?",
                                               (budget._TRUE_DOWN_CONV, reconcile_guard_failed_provider))}
assert reconcile_guard_readable_actual == {k: v for k, v in reconcile_guard_reference.items()
                                           if k[1] != reconcile_guard_failed_provider}
reconcile_guard_partial = ledger_sync.reconcile_into_ledger(RECONCILE_GUARD_SINCE)
assert not reconcile_guard_partial["complete"]
assert reconcile_guard_partial["gap_rebuild_refused"]
assert reconcile_guard_gap_rows == [(r["day"], r["project_primary"], Decimal(str(r["batch_usd"])))
    for r in reconcile_guard_connection.execute("SELECT * FROM spend_events WHERE reconciled=1")]
print("OK reconcile_read_containment: complete full-scan corrections and late-row refusal with readable remainder")
