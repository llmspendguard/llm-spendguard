"""Guard: `claude-code invoices` ingests the REAL Anthropic charges (a by-card CSV) into spend_events as the
ground-truth billed stream, split into the three streams — never conflated:
  • the recurring monthly $200 claude.ai charge = base SUBSCRIPTION → subscription_usd.
  • every other claude.ai charge = REAL Claude Code OVERAGE → realtime_usd, source='anthropic-invoice'.
  • Console/API credit grants → realtime_usd, source='anthropic-invoice-api' (the API PURCHASE side, kept separate
    so it is never summed into the overage).
Idempotent on dedup_key='inv:<invoice_id>'. Hermetic: isolated home + a fabricated CSV. Zero spend."""
import os, sys, tempfile, sqlite3

if not os.environ.get("SPENDGUARD_TEST_ISOLATED"):
    home = tempfile.mkdtemp(prefix="spendguard-ccinv-")
    os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"
    os.environ["SPENDGUARD_HOME"] = home
    os.execv(sys.executable, [sys.executable] + sys.argv)

from spendguard import claudecode, budget, config

fails = []
def ck(name, cond):
    print(("  [OK] " if cond else "  [FAIL] ") + name)
    if not cond:
        fails.append(name)

CSV = os.path.join(os.environ["SPENDGUARD_HOME"], "charges.csv")
with open(CSV, "w") as f:
    f.write("Card,Reimburse,Source,Invoice ID,Date,Type,Amount\n")
    f.write("•••• 0521,Yes,claude.ai,INV-1,2026-04-06,,200.00\n")     # first $200 of Apr → base subscription
    f.write("•••• 0521,Yes,claude.ai,INV-2,2026-04-10,,200.00\n")     # 2nd $200 → overage bundle
    f.write("•••• 0521,Yes,claude.ai,INV-3,2026-04-12,,45.00\n")      # overage top-up
    f.write("•••• 1955,No,Console/API,INV-4,2026-04-15,Credit grant,50.00\n")  # API purchase side
    f.write(",,,,,subtotal (4 invoices),495.00\n")                    # a summary row — must be skipped

claudecode.ingest_invoices(csv_path=CSV)

con = sqlite3.connect(config.db_path())
def q(sql):
    return con.execute(sql).fetchone()

sub = q("SELECT COALESCE(SUM(CAST(subscription_usd AS REAL)),0) FROM spend_events WHERE source='anthropic-invoice'")[0]
over = q("SELECT COALESCE(SUM(CAST(realtime_usd AS REAL)),0) FROM spend_events WHERE source='anthropic-invoice'")[0]
api = q("SELECT COALESCE(SUM(CAST(realtime_usd AS REAL)),0) FROM spend_events WHERE source='anthropic-invoice-api'")[0]
n, uniq = q("SELECT COUNT(*), COUNT(DISTINCT dedup_key) FROM spend_events WHERE source LIKE 'anthropic-invoice%'")
billed = q("SELECT COALESCE(SUM(billed),0), COUNT(*) FROM spend_events WHERE source LIKE 'anthropic-invoice%'")

ck("base SUBSCRIPTION = $200 (the first $200/month) → subscription_usd", round(sub, 2) == 200.0)
ck("REAL Claude Code OVERAGE = $245 (2nd $200 bundle + $45) → realtime_usd on source=anthropic-invoice",
   round(over, 2) == 245.0)
ck("API credit grant = $50 → separate source=anthropic-invoice-api (never summed into overage)", round(api, 2) == 50.0)
ck("the summary/subtotal row was skipped (exactly 4 charges booked)", n == 4)
ck("every invoice row is billed=1", billed[0] == billed[1] == 4)
ck("idempotency invariant: rows == distinct dedup_key", n == uniq == 4)

claudecode.ingest_invoices(csv_path=CSV)                    # re-run
n2 = q("SELECT COUNT(*) FROM spend_events WHERE source LIKE 'anthropic-invoice%'")[0]
ck("re-ingest is idempotent (delete+rebook) — still 4 rows, not 8", n2 == 4)
con.close()

print(("[OK]" if not fails else "[FAIL]") + " claude-code-invoice-ingest: %d failure(s)" % len(fails))
sys.exit(1 if fails else 0)
