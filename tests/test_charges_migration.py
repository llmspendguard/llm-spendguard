"""charges → spend_events migration — the never-lose-a-dollar guard.

Seeds the legacy `charges` ledger with the tricky cases (zero-cost, a duplicate-cost PAIR that must both survive,
a reconciliation MARKER row, a meta row) and asserts the migration into spend_events is:
  • Σ-CONSERVING — every charge dollar lands, exactly (integer micros, delta 0),
  • IDEMPOTENT — re-running books nothing new (dedup by source rowid; no double-count),
  • FAITHFUL — meta stays is_meta, the marker row is reconciled, attribution maps repo→org (lmm→healiom),
  • AUDITED — the hash-chained spend_audit verifies.

Offline, isolated home, zero spend.
"""
import os, sys, tempfile

if not os.environ.get("SPENDGUARD_TEST_ISOLATED"):
    os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"
    os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="spendguard-mig-")
    os.execv(sys.executable, [sys.executable] + sys.argv)

from spendguard import budget, conv, migrate_charges
from spendguard import ledger as L

fails = []
def ck(name, cond):
    print(("  [OK] " if cond else "  [FAIL] ") + name)
    if not cond:
        fails.append(name)

# isolated home has no taxonomy/transcripts → pin the repo→org map + make resolve hermetic (no real transcript reads)
conv._prior_index = lambda: {"lmm": ("healiom", "lmm"), "manga2anime": ("ensight", "manga2anime"),
                             "llmseg": ("ensight", "llm-spendguard")}
conv.segments = lambda *a, **k: []
conv._seg_get_all = lambda: {}

db = budget._db()
def ins(ts, provider, model, kind, cost, project, conv_id="c1"):
    db.execute("INSERT INTO charges (ts,day,provider,model,kind,cost,project,conv_id) VALUES (?,?,?,?,?,?,?,?)",
               (ts, ts[:10], provider, model, kind, cost, project, conv_id))

ins("2026-06-01T10:00:00+00:00", "openai", "gpt-5.5", "realtime", 1.50, "lmm")
ins("2026-06-01T11:00:00+00:00", "anthropic", "claude-haiku-4-5", "batch", 2.00, "manga2anime")
ins("2026-06-01T12:00:00+00:00", "anthropic", "claude-opus-4-8", "meta", 0.25, "llmseg")
ins("2026-06-02T00:00:00+00:00", "openai", "(provider-batch)", "batch", 10.00, "lmm")   # reconciliation MARKER
ins("2026-06-03T09:00:00+00:00", "openai", "gpt-5.5", "realtime", 0.75, "lmm")           # dup-cost pair A
ins("2026-06-03T09:00:00+00:00", "openai", "gpt-5.5", "realtime", 0.75, "lmm")           # dup-cost pair B (diff rowid)
ins("2026-06-03T10:00:00+00:00", "openai", "gpt-5.5", "realtime", 0.00, "lmm")           # zero → skipped
db.commit()
SRC_TOTAL = 1.50 + 2.00 + 0.25 + 10.00 + 0.75 + 0.75                                      # 15.25

led = L.SpendLedger()
st = migrate_charges.to_spend_events(led=led)
ck("Σ conserved — delta 0 (no dollar lost/added)", abs(st["delta_usd"]) < 1e-6)
ck("migrated 6 nonzero rows, skipped the 1 zero", st["migrated"] == 6 and st["skipped_zero"] == 1)
ck("dst total == src total == 15.25", abs(st["dst_total_usd"] - SRC_TOTAL) < 1e-6 and abs(st["src_total_usd"] - SRC_TOTAL) < 1e-6)

rows = led.query(where={"source": "migrate:charges"})
ck("duplicate-cost pair BOTH survived (rowid dedup, not value dedup)",
   len([r for r in rows if r["realtime_micros"] == 750000]) == 2)
metas = [r for r in rows if r.get("is_meta")]
ck("meta row flagged is_meta + booked as realtime micros", len(metas) == 1 and metas[0]["realtime_micros"] == 250000)
recon = [r for r in rows if r.get("reconciled")]
ck("marker row → reconciled + status=reconciled + batch micros", len(recon) == 1
   and recon[0]["status"] == "reconciled" and recon[0]["batch_micros"] == 10000000)
ck("attribution lmm → org healiom", any(r["org"] == "healiom" and r["project_primary"] == "lmm" for r in rows))
ck("attribution manga2anime → org ensight", any(r["org"] == "ensight" and r["batch_micros"] == 2000000 for r in rows))

# idempotency: a SECOND migration (fresh ledger handle, same db) must book NOTHING new
st2 = migrate_charges.to_spend_events(led=L.SpendLedger())
rows2 = L.SpendLedger().query(where={"source": "migrate:charges"})
ck("idempotent — re-run leaves 6 rows + same $ (dedup, no double-count)",
   len(rows2) == 6 and abs(st2["dst_total_usd"] - SRC_TOTAL) < 1e-6)

ok, bad = led.verify_audit_chain()
ck("spend_audit hash chain intact after bulk migration", ok)

print(("[OK]" if not fails else "[FAIL]") + " charges-migration: %d failure(s)" % len(fails))
sys.exit(1 if fails else 0)
