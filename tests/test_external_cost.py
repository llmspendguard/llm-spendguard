"""Non-token EXTERNAL cost (MCP/tool calls + external paid APIs) as a first-class REAL-$ ledger axis.

The Revenium borrow, spendguard-shaped: a paid call that isn't an LLM (an MCP tool, a Stripe/Twilio/search API) is
recorded on its OWN `external` axis — real money out the door, kept APART from LLM token spend, GPU/remote, and the
flat subscription fee, and NEVER summed into the est-value axis. Guards the full "add a real-$ axis" wiring:
  1. record_external_cost lands on external_usd (the external axis), attributed to the trace, and is BILLED (real $).
  2. it is NOT the LLM cap number (spent_since reads only batch+realtime) — a tool cost never inflates LLM spend.
  3. the receipt's REAL-$ total includes it as a 4th named component (API + Subscription + Remote + External).
  4. the FOCUS export files it under ServiceCategory "Other" (a tool/API is not an AI/ML service), BilledCost set.

Offline, isolated home, zero LLM spend, no network."""
import os, sys, tempfile

if not os.environ.get("SPENDGUARD_TEST_ISOLATED"):
    os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"
    os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="spendguard-external-")
    os.execv(sys.executable, [sys.executable] + sys.argv)

from spendguard import budget, receipt, focus_export
from spendguard import ledger as _ledger

fails = []
def ck(name, cond):
    print(("  [OK] " if cond else "  [FAIL] ") + name)
    if not cond:
        fails.append(name)

SINCE = "2026-06-01"

print("-- (1) record_external_cost lands on the external axis, attributed, and BILLED --")
budget.record_external_cost("stripe", "charge.create", 0.30, conv_id="c1", intent="checkout", project="shopapp")
budget.record_external_cost("mcp:websearch", "search", 0.02, conv_id="c1", intent="research", project="shopapp")
rows = [e for e in budget._ledger().query(since=SINCE) if e.get("external_usd")]
ck("both external rows recorded on external_usd", len(rows) == 2)
r0 = next((e for e in rows if e.get("model") == "charge.create"), None)
ck("the service is named (model slot), provider carried", r0 and r0.get("provider") == "stripe")
ck("attributed to the trace (conv/intent/project) like any charge",
   r0 and r0.get("conv_id") == "c1" and r0.get("intent") == "checkout" and r0.get("project_primary") == "shopapp")
ck("external_usd is a BILLED (real-$) column, not est-value",
   "external_usd" in _ledger.BILLED_USD_COLS and "external_usd" not in _ledger.LLM_USD_COLS)

print("-- (2) external is NOT the LLM cap number (a tool cost never inflates LLM spend) --")
ck("spent_since (LLM cap = batch+realtime) is $0 — external excluded", budget.spent_since(SINCE) == 0.0)
ck("by_day(kind='external') sums the external axis to $0.32",
   abs(sum(budget.by_day(kind="external", since=SINCE).values()) - 0.32) < 1e-9)

print("-- (3) the receipt REAL-$ total includes External as a 4th named component --")
t = receipt.tally()
ck("tally exposes the external axis", (t.get("external") or {}).get("month") is not None)
ck("external month == $0.32", abs((t["external"]["month"]) - 0.32) < 1e-9)
expected_real = ((t["api"].get("month") or 0) + (t.get("subscription") or 0)
                 + ((t.get("remote") or {}).get("month") or 0) + 0.32)
ck("real_month = API + Subscription + Remote + External (the 4-part REAL-$)",
   abs(t["real_month"] - expected_real) < 1e-6)
line = receipt._tally_lines(t)[0]
ck("the receipt line SHOWS 'external $' as a named component", "external $" in line)
ck("...still on the REAL-$ axis, never summed with est-value ('::' separates them)",
   "real $ this month" in line and ":: " not in line)
# EVERY REAL-$ renderer must show it (the 5-place-checklist: a new axis missed in one renderer under-reports $)
table = "\n".join(receipt._two_axis_table(t))
ck("the receipt TABLE renderer shows an External row", "External" in table)
ck("the compact status line renderer shows external", "external " in receipt.render_line(t))

print("-- (4) FOCUS export files external as ServiceCategory 'Other', BilledCost set --")
fx = focus_export.focus_row(r0)
ck("kind external → SkuId names the service:external", fx["SkuId"] == "charge.create:external")
ck("ServiceCategory 'Other' (a tool/API is not an AI/ML service)", fx["ServiceCategory"] == "Other")
ck("ServiceName is the tool/endpoint, not 'provider API'", fx["ServiceName"] == "charge.create")
ck("BilledCost set (real $), ChargeCategory Usage", fx["BilledCost"] and fx["ChargeCategory"] == "Usage")
# and the whole ledger still projects — external rows don't break the export
allrows = focus_export.export_rows(since=SINCE)
ck("export projects every row incl. the external ones", len(allrows) == len(budget._ledger().query(since=SINCE)))

print(("\n[OK] " if not fails else "\n[FAIL] ") + f"external_cost: {len(fails)} failure(s)")
sys.exit(1 if fails else 0)
