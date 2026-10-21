"""Design item (P7-ledger, caller-intent): an UNPRICED provider batch prices to $0, and the reconcile treated that $0
as REAL — corrupting two money paths:
  · true_down: an unpriced batch counted as $0 UNDERSTATES the billed total → an INFLATED est−billed delta → a too-large
    (wrong) correction written to the ledger. Now the (provider, model) is marked incomplete, SKIPPED for the true-down,
    and surfaced in `unpriced_incomplete` — never trued down against an unknown.
  · _provider_total: an unpriced model made the OpenAI sum incomplete, but the total was returned as authoritative TRUTH
    (a reconciled-looking partial). Now it returns None (UNKNOWN), the same as a fetch failure.

Offline ($0): billed_rows + the provider readers are faked. Isolation: SPENDGUARD_HOME → mkdtemp before import.
"""
import os, sys, tempfile
from types import MappingProxyType

RECONCILE_POSTING_METADATA_FIELDS = frozenset(("recorded_at", "tz", "local_datetime"))

os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="spendguard-reconcile-unpriced-")

from spendguard import ledger_sync, budget, pricing   # noqa: E402
from _script_checks import seed_test_spend_event   # noqa: E402
from spendguard.reconcile_maintenance_owner import ReconcileMaintenanceConnection   # noqa: E402


def main():
    fails = 0

    def ck(name, cond, extra=""):
        nonlocal fails
        if not cond:
            fails += 1
        print(f"  [{'OK' if cond else 'FAIL'}] {name}{('  — ' + str(extra)) if extra and not cond else ''}")

    # ── true_down: an unpriced batch (cost=None) is EXCLUDED from the correction, never trued down against unknown ──
    _real_record = budget.record_true_down
    recorded = []
    maintenance_owners = []
    reconcile_posting_snapshots = []
    # Reconciliation now streams the ledger; seed its actual input rather than
    # mocking the retired aggregate-reader seam (which created no database).
    seed_test_spend_event("2026-06-15", "openai", "gpt-5.5", "batch", 10.0, "lmm",
                          timestamp_suffix="T00:00:00+00:00", source="unpriced-billed-fixture",
                          dedup_fields=("day", "provider", "model"))
    def capture_reconcile_unpriced_owned_correction(day, prov, model, amt, project=None, *,
                                                   maintenance_ledger, posting_metadata=None):
        recorded.append((prov, model, amt))
        maintenance_owners.append(maintenance_ledger._conn)
        reconcile_posting_snapshots.append(posting_metadata)
        return _real_record(day, prov, model, amt, project, maintenance_ledger=maintenance_ledger,
                            posting_metadata=posting_metadata)

    budget.record_true_down = capture_reconcile_unpriced_owned_correction
    try:
        # UNPRICED billed batch (cost=None): its $ is UNKNOWN, must NOT be read as $0 (which would make delta=10 → a
        # wrong $10 correction). billed_rows row shape: (provider, model, cost, in_tok, out_tok, day, batch_id).
        recorded.clear()
        out = ledger_sync.true_down(since="2026-06-01",
                                    billed_rows={"openai": [("openai", "gpt-5.5", None, 0, 0, "2026-06-15", "b1")]})
        ck("an unpriced batch records NO true-down (never corrected against an unknown billed total)", recorded == [], recorded)
        _ui = out.get("unpriced_incomplete") or []   # entry is "openai:<normalized model>" — assert the seam, not the alias
        ck("the unpriced model is SURFACED in unpriced_incomplete (one openai entry)",
           len(_ui) == 1 and _ui[0].startswith("openai:"), out)
        ck("trued_down is 0 for the unpriced-only case", out.get("trued_down") == 0, out)

        # contrast: a PRICED billed batch ($8) DOES true down the delta ($10 est − $8 billed = $2)
        recorded.clear()
        out2 = ledger_sync.true_down(since="2026-06-01",
                                     billed_rows={"openai": [("openai", "gpt-5.5", 8.0, 0, 0, "2026-06-15", "b2")]})
        ck("a PRICED batch still trues down the real delta (est − billed)", recorded and abs(recorded[0][2] - 2.0) < 1e-6, recorded)
        ck("priced corrections use the explicitly owned offline maintenance connection",
           maintenance_owners and all(isinstance(owner, ReconcileMaintenanceConnection) for owner in maintenance_owners))
        ck("its unpriced_incomplete is empty", out2.get("unpriced_incomplete") == [], out2)
        ck("the priced correction carries complete immutable operation metadata",
           len(reconcile_posting_snapshots) == len(recorded)
           and all(isinstance(snapshot, MappingProxyType)
                   and RECONCILE_POSTING_METADATA_FIELDS <= snapshot.keys()
                   and all(snapshot[field] for field in RECONCILE_POSTING_METADATA_FIELDS)
                   for snapshot in reconcile_posting_snapshots), reconcile_posting_snapshots)
    finally:
        budget.record_true_down = _real_record

    # ── _provider_total: an unpriced model in openai_by_day → the total is UNKNOWN (None), not a partial masquerade ──
    import spendguard.report as report
    import spendguard.reconcile_anthropic as anth
    _real_oai, _real_anth = report.openai_by_day, anth.cost_by_day
    anth.cost_by_day = lambda since=None: ({"2026-06-15": 0.0}, [])

    def _oai_unpriced():                                  # simulates openai_by_day hitting an unpriced model (records it)
        pricing.note_unpriced("some-unpriced-model")
        return ({"2026-06-15": 3.0}, 0)
    report.openai_by_day = _oai_unpriced
    try:
        ck("_provider_total → None (UNKNOWN) when an unpriced model made the sum incomplete",
           ledger_sync._provider_total("2026-06-01") is None)
        report.openai_by_day = lambda: ({"2026-06-15": 3.0}, 0)   # NO unpriced → a real total
        ck("_provider_total → the real total when nothing is unpriced",
           ledger_sync._provider_total("2026-06-01") == 3.0, ledger_sync._provider_total("2026-06-01"))
    finally:
        report.openai_by_day, anth.cost_by_day = _real_oai, _real_anth

    print(f"\n{'[FAIL]' if fails else '[OK]'} test_reconcile_unpriced_not_zero: {fails} failure(s)")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
