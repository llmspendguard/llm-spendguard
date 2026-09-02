"""Phase-2 guard: `claude-code overflow` reconstructs billing_state — which app turns billed for REAL once the
weekly plan cap was exhausted — from a LOCAL cap-fill, never the Admin API. Pins the crisis-fix invariants:

  1. NO cap declared → NOTHING is reconstructed as overflow (no fabrication — the safe, honest default).
  2. With a declared weekly cap, a time-ordered cap-fill marks only the slice PAST the cap as overflow, attributes
     it to the exact conversation whose turn crossed, and books it as a SEPARATE realtime/billed stream
     (source="claude-code-overflow", realtime_usd, billed=1).
  3. The est-value stream is UNTOUCHED — est_value_dec is unchanged. The two axes are never summed:
     est-value = SUM(est_chat_usd) on source='claude-code'; real overflow = SUM(realtime_usd) on the overflow source.
  4. It is a RECONCILIATION: re-running is idempotent (delete+rebook, $50 stays $50), and a CHANGED cap re-derives
     the overflow correctly (never stale).

Hermetic: isolated SPENDGUARD_HOME, seeded est_chat rows with fixed timestamps in ONE anchored window. Zero spend."""
import os, sys, tempfile, sqlite3
from decimal import Decimal

if not os.environ.get("SPENDGUARD_TEST_ISOLATED"):
    home = tempfile.mkdtemp(prefix="spendguard-ccoverflow-")
    os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"
    os.environ["SPENDGUARD_HOME"] = home
    os.execv(sys.executable, [sys.executable] + sys.argv)

from spendguard import claudecode, budget, config

fails = []
def ck(name, cond):
    print(("  [OK] " if cond else "  [FAIL] ") + name)
    if not cond:
        fails.append(name)

ANCHOR = "2026-08-24T00:00:00+00:00"                                   # a known window boundary → deterministic weeks

def seed(conv, ts, val, mid):
    budget._record_spend_event("anthropic", "claude-haiku-4-5", "est_chat", float(val),
                               conv_id=conv, occurred_at=ts, source="claude-code", basis="reconstructed",
                               project="claude-code", dedup_key="cc:" + mid)

# three turns in ONE window, in time order: convA 100 + 100, then convB 100 (latest). Total $300.
seed("convA", "2026-08-24T01:00:00+00:00", 100, "s1")
seed("convA", "2026-08-24T02:00:00+00:00", 100, "s2")
seed("convB", "2026-08-24T03:00:00+00:00", 100, "s3")

led = budget._ledger()
SINCE = "2026-01-01"

def overflow_rows():
    con = sqlite3.connect(config.db_path())
    try:
        return con.execute("SELECT count(*), COALESCE(SUM(CAST(realtime_usd AS REAL)),0), COALESCE(SUM(billed),0) "
                           "FROM spend_events WHERE source='claude-code-overflow'").fetchone()
    finally:
        con.close()

# 1. no cap declared (config unset in this isolated home) → NOTHING reconstructed
claudecode.reconcile_overflow(cap_usd=None)
ck("cap unset → NO overflow rows (no fabrication)", claudecode.overflow_by_conversation() == {})

# 2. declared cap $250 → only the $50 past the cap overflows, attributed to convB (whose turn crossed)
claudecode.reconcile_overflow(cap_usd=250, anchor=ANCHOR)
ov = claudecode.overflow_by_conversation()
ck("overflow total = $50 (300 − 250)", round(sum(ov.values()), 2) == 50.0)
ck("the $50 is attributed to convB — the conversation whose turn crossed the cap", round(ov.get("convB", 0), 2) == 50.0)
ck("convA (entirely under the cap) has no overflow", round(ov.get("convA", 0.0), 2) == 0.0)
n, rt, bl = overflow_rows()
ck("overflow booked as realtime_usd, billed=1 (real $, not est-value)", round(rt, 2) == 50.0 and bl == n and n >= 1)

# 3. the SPLIT — est-value stream untouched; the two axes are separate
ck("est-value stream UNTOUCHED — est_value_dec still $300 (overflow did not move it)",
   Decimal(led.est_value_dec(since=SINCE)) == Decimal("300"))

# 4a. idempotent re-run (delete+rebook) — still $50, never $100
claudecode.reconcile_overflow(cap_usd=250, anchor=ANCHOR)
ck("re-reconcile is idempotent — still $50, not doubled", round(sum(claudecode.overflow_by_conversation().values()), 2) == 50.0)

# 4b. a CHANGED cap re-derives correctly — cap $150 → $150 overflow (convA turn2 $50 + convB turn3 $100)
claudecode.reconcile_overflow(cap_usd=150, anchor=ANCHOR)
ov3 = claudecode.overflow_by_conversation()
ck("cap change re-reconciles (never stale): cap $150 → $150 overflow, convA $50 + convB $100",
   round(sum(ov3.values()), 2) == 150.0 and round(ov3.get("convA", 0), 2) == 50.0 and round(ov3.get("convB", 0), 2) == 100.0)
ck("est-value STILL $300 after cap change (est stream is never touched by reconcile)",
   Decimal(led.est_value_dec(since=SINCE)) == Decimal("300"))

print(("[OK]" if not fails else "[FAIL]") + " claude-code-overflow-reconcile: %d failure(s)" % len(fails))
sys.exit(1 if fails else 0)
