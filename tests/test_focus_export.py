"""FOCUS export (focus_export) + the reconcile-path invoice_id stamp.

Guards two Tier-1 changes together, because they're one feature — making the ledger reconcile at FinOps grade:
  1. focus_export.focus_row projects a spend_events row to FOCUS 1.2, and the money lands on the RIGHT axis:
     a billed usage row -> BilledCost; an estimate -> ListCost with NO InvoiceId (it was never billed); a
     subscription -> Purchase; est_chat -> BilledCost 0 + EffectiveCost 'covered-by-subscription' (the est-value
     axis, never summed into billed); a true-down -> ChargeClass 'Correction'.
  2. the reconcile writers stamp invoice_id = provider:period ONLY on the rows they write (reconciled + true-down),
     so the FOCUS reconciliation anchor is populated by reconciliation and an unreconciled estimate stays NULL.

Offline, isolated home, zero spend, no network."""
import os, sys, tempfile, json

if not os.environ.get("SPENDGUARD_TEST_ISOLATED"):
    os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"
    os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="spendguard-focus-")
    os.execv(sys.executable, [sys.executable] + sys.argv)

from spendguard import budget, focus_export
from spendguard import ledger_sync as LS

fails = []
def ck(name, cond):
    print(("  [OK] " if cond else "  [FAIL] ") + name)
    if not cond:
        fails.append(name)

SINCE = "2026-06-01"
D1 = "2026-06-14"
OPUS = "claude-opus-4-8"


def rows_where(**pred):
    out = []
    for ev in budget._ledger().query(since=SINCE):
        if all((ev.get(k) == v) for k, v in pred.items()):
            out.append(ev)
    return out


# ── seed on a FIXED day with distinct dedup keys (same-second record_charge() calls would collide on dedup_key and
#    merge). Seeds the ORIGINAL rows; the reconcile writers add the stamped correction/reconciled rows below. ──
def seed(model, kind, cost, basis=""):
    ev = budget.charge_to_event("anthropic", model, kind, float(cost), basis=basis)
    ev["project_primary"] = "lmm"; ev["projects"] = ["lmm"]
    ev["occurred_at"] = ev["ts_utc"] = D1 + "T12:00:00+00:00"          # noon UTC → no reporting-tz day shift
    ev["source"] = ev["recorded_by"] = "test"
    ev["dedup_key"] = "test:%s:%s:%s:%s" % (D1, model, kind, cost)
    budget._ledger().record_event(ev)

seed(OPUS, "realtime", 0.50, basis=budget.BASIS_BILLED)               # actual spend → BilledCost
seed(OPUS, "batch", 100.0, basis=budget.BASIS_ESTIMATE)              # pre-submit projection → ListCost
seed("claude-max", "subscription", 200.0)                            # the flat plan fee → Purchase (real $)
seed(OPUS, "est_chat", 9.99)                                        # plan-covered usage → est-value, never billed

# ── true_down: provider billed only $70 of the $100 batch estimate → a correction row is written + stamped ──
LS.true_down(since=SINCE, billed_rows={"anthropic": [("anthropic", OPUS, 70.0, 1_000_000, 50_000, D1, "b-a1")]})
# ── a reconciled gap row (provider-billed spend the gate never saw) ──
budget.record_reconciled(D1, "anthropic", 12.34, project="lmm")

print("-- (1) invoice_id is stamped provider:period ONLY on reconcile-written rows --")
corr = rows_where(conv_id=budget._TRUE_DOWN_CONV)
ck("true-down correction row exists", len(corr) >= 1)
ck("...and carries invoice_id = 'anthropic:2026-06'", corr and corr[0].get("invoice_id") == "anthropic:2026-06")
recon = rows_where(recon_marker=budget._RECONCILED)
ck("reconciled gap row carries invoice_id = 'anthropic:2026-06'",
   recon and recon[0].get("invoice_id") == "anthropic:2026-06")
est = [e for e in rows_where() if (e.get("cost_basis") or "") == budget.BASIS_ESTIMATE and e.get("conv_id") != budget._TRUE_DOWN_CONV]
ck("the ORIGINAL estimate row was NOT mutated — its invoice_id stays NULL (never billed)",
   est and not est[0].get("invoice_id"))

print("-- (2) focus_row lands money on the right FOCUS axis --")
def one(**pred):
    r = rows_where(**pred)
    return focus_export.focus_row(r[0]) if r else None

fb = one(cost_basis=budget.BASIS_BILLED)
ck("billed usage → BilledCost set, ChargeCategory Usage", fb and fb["BilledCost"] and fb["ChargeCategory"] == "Usage")
fe = focus_export.focus_row(est[0])
ck("estimate → ListCost set, BilledCost None, InvoiceId None (never billed)",
   fe["ListCost"] and fe["BilledCost"] is None and fe["InvoiceId"] is None)
fc = focus_export.focus_row(corr[0])
ck("correction → ChargeClass 'Correction' + InvoiceId anchored", fc["ChargeClass"] == "Correction" and fc["InvoiceId"] == "anthropic:2026-06")
# est_chat + subscription rows — find by which axis actually CARRIES money (the ledger zero-fills the other
# category columns with the string "0", which is truthy, so a raw e.get(col) picks the wrong row).
def by_axis(kind):
    return next((focus_export.focus_row(e) for e in rows_where() if focus_export._charge_axis(e)[0] == kind), None)
fchat = by_axis("est_chat")
ck("est_chat → BilledCost 0 + EffectiveCost 'covered-by-subscription' (est-value, never a $ out)",
   fchat and fchat["BilledCost"] == "0" and fchat["EffectiveCost"] == "covered-by-subscription")
fsub = by_axis("subscription")
ck("subscription → ChargeCategory Purchase, ChargeFrequency Recurring",
   fsub and fsub["ChargeCategory"] == "Purchase" and fsub["ChargeFrequency"] == "Recurring")

print("-- (3) export + reconciliation summary + CSV shape --")
rows = focus_export.export_rows(since=SINCE)
ck("export projects every ledger row", len(rows) == len(budget._ledger().query(since=SINCE)))
summ = focus_export.reconciliation_summary(rows)
ck("summary reports >=1 invoice-anchored row", summ["rows_invoice_anchored"] >= 2)
ck("summary reports the correction", summ["correction_rows"] >= 1)
ck("summary keeps est-value on its own axis (not summed into billed)", summ["est_value_rows"] >= 1)
# BilledCost = the REAL dollars (realtime 0.50 + subscription 200 + reconciled 12.34) NET of the true-down
# correction (−30); the batch ESTIMATE is ListCost not billed, and est_chat is est-value not billed. Corrections
# net exactly like the ledger, and the est-value axis is never summed in.
from decimal import Decimal
ck("BilledCost = real-$ (0.50 + 200 + 12.34) net of the −30 correction = 182.84",
   Decimal(summ["billed_cost"]) == Decimal("182.84"))
csv_out = focus_export.emit_csv(rows)
ck("CSV header is exactly FOCUS_COLUMNS", csv_out.splitlines()[0] == ",".join(focus_export.FOCUS_COLUMNS))
import csv as _csv, io as _io
_parsed = list(_csv.DictReader(_io.StringIO(csv_out)))
ck("CSV has one data row per ledger row", len(_parsed) == len(rows))
ck("CSV Tags column is valid JSON", all(isinstance(json.loads(r["Tags"]), dict) for r in _parsed))
ck("JSON emit round-trips", isinstance(json.loads(focus_export.emit_json(rows)), list))

print(("\n[OK] " if not fails else "\n[FAIL] ") + f"focus_export: {len(fails)} failure(s)")
sys.exit(1 if fails else 0)
