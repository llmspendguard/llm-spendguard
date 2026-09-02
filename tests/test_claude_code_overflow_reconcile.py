"""Phase-2 guard (window-segmentation model): `claude-code overflow` CALCULATES billing_state by SEGMENTING each
timeline into subscription vs overage WINDOWS, from the PROVIDER's own observable signals — never a guessed cap,
never the admin API. Est-value is the frame ONLY inside a subscription window; a turn PAST the weekly cap (until
reset) ran on paid credit = REAL PAID tokens. Pins:

  1. The weekly RESET GRID comes from the provider's `resetsAt` (+7d); the cap-hit boundary B from a seven_day
     quotaLimits / 429 "weekly limit". An overage window is [B, next reset).
  2. A turn INSIDE an overage window is booked as REAL PAID (realtime/billed=1, source=claude-code-overflow) at its
     full est-value — the UNRECONCILED upper bound. A turn before B or after the reset stays plan-covered est-value.
  3. Plan-covered est-value = total est_chat − overage (est-value is subscription-windows-only). The two axes are
     never summed. Idempotent (delete+rebook).
  4. No observable weekly reset grid + cap-hit → nothing is segmented as overage (all plan-covered).

Hermetic: isolated SPENDGUARD_HOME + a fabricated transcript carrying the provider limit signals + seeded est_chat
rows straddling the window. Zero spend."""
import os, sys, tempfile, json, datetime, sqlite3
from decimal import Decimal

if not os.environ.get("SPENDGUARD_TEST_ISOLATED"):
    home = tempfile.mkdtemp(prefix="spendguard-ccwin-")
    os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"
    os.environ["SPENDGUARD_HOME"] = home
    os.environ["SPENDGUARD_CC_DIR"] = os.path.join(home, "projects")
    os.execv(sys.executable, [sys.executable] + sys.argv)

from spendguard import claudecode, budget, config

fails = []
def ck(name, cond):
    print(("  [OK] " if cond else "  [FAIL] ") + name)
    if not cond:
        fails.append(name)

UTC = datetime.timezone.utc
RESET = datetime.datetime(2026, 8, 24, 16, 0, 0, tzinfo=UTC).timestamp()   # the week's reset (provider resetsAt)

CC = os.path.join(os.environ["SPENDGUARD_CC_DIR"], "proj")
os.makedirs(CC, exist_ok=True)
with open(os.path.join(CC, "sess.jsonl"), "w") as f:
    # a seven_day cap-hit at 10:00 on Aug 22 (boundary B) carrying the provider's weekly resetsAt (the grid anchor)
    f.write(json.dumps({"timestamp": "2026-08-22T10:00:00Z",
                        "quotaLimits": {"rateLimitType": "seven_day", "resetsAt": RESET, "status": "rejected",
                                        "overageStatus": "rejected", "overageDisabledReason": "out_of_credits",
                                        "isUsingOverage": False}}) + "\n")
    # a separate five_hour rejection (friction) — must NOT create a weekly window
    f.write(json.dumps({"timestamp": "2026-08-23T01:00:00Z",
                        "quotaLimits": {"rateLimitType": "five_hour", "status": "rejected",
                                        "overageStatus": "rejected", "overageDisabledReason": "out_of_credits"}}) + "\n")

def seed(conv, ts, val, mid):
    budget._record_spend_event("anthropic", "claude-haiku-4-5", "est_chat", float(val),
                               conv_id=conv, occurred_at=ts, source="claude-code", project="claude-code",
                               dedup_key="cc:" + mid)
seed("convA", "2026-08-22T09:00:00+00:00", 50, "before")    # BEFORE B → subscription
seed("convA", "2026-08-22T11:00:00+00:00", 100, "inA")      # inside [B, RESET) → overage
seed("convB", "2026-08-23T12:00:00+00:00", 30, "inB")       # inside [B, RESET) → overage
seed("convB", "2026-08-24T20:00:00+00:00", 20, "after")     # AFTER the reset → subscription

# 1. observable signals + window construction
ev = claudecode._overage_events()
ck("provider weekly reset grid captured (resetsAt from the seven_day record)", RESET in ev["weekly_resets"])
ck("seven_day cap-hit captured as a weekly-hit boundary", len(ev["weekly_hits"]) >= 1)
wins, anchor = claudecode._overage_windows(ev)
ck("exactly ONE overage window [B, reset) is built (five_hour does NOT make a window)", len(wins) == 1)

# 2. reclassify by window: only the two in-window turns are real paid
claudecode.reconcile_billing_state()
ov = claudecode.overflow_by_conversation()
ck("overage = $130 upper bound (the two in-window turns: convA $100 + convB $30)", round(sum(ov.values()), 2) == 130.0)
ck("convA in-window turn billed $100 (its BEFORE-window $50 is not)", round(ov.get("convA", 0), 2) == 100.0)
ck("convB in-window turn billed $30 (its AFTER-reset $20 is not)", round(ov.get("convB", 0), 2) == 30.0)
con = sqlite3.connect(config.db_path())
n, rt, bl = con.execute("SELECT count(*), COALESCE(SUM(CAST(realtime_usd AS REAL)),0), COALESCE(SUM(billed),0) "
                        "FROM spend_events WHERE source='claude-code-overflow'").fetchone()
con.close()
ck("overage booked as realtime_usd, billed=1", round(rt, 2) == 130.0 and bl == n and n >= 1)

# 3. the split: gross est_chat unchanged; plan-covered = gross − overage (est-value is subscription-windows-only)
led = budget._ledger()
gross = Decimal(led.est_value_dec(since="2026-01-01"))
ck("gross est_chat still $200 (the ingest is not mutated)", gross == Decimal("200"))
ck("plan-covered = gross − overage = $70 (subscription windows only)", gross - Decimal("130") == Decimal("70"))

# 4. idempotent
claudecode.reconcile_billing_state()
ck("re-reconcile is idempotent — still $130", round(sum(claudecode.overflow_by_conversation().values()), 2) == 130.0)

# 5. no observable grid + cap-hit → nothing is overage (drop the quota signals)
with open(os.path.join(CC, "sess.jsonl"), "w") as f:
    f.write(json.dumps({"timestamp": "2026-08-23T01:00:00Z", "type": "user",
                        "message": {"role": "user", "content": "hi"}}) + "\n")
claudecode.reconcile_billing_state()
ck("no weekly reset grid + cap-hit observed → $0 overage (all plan-covered)",
   round(sum(claudecode.overflow_by_conversation().values()), 2) == 0.0)

print(("[OK]" if not fails else "[FAIL]") + " claude-code-billing-state-windows: %d failure(s)" % len(fails))
sys.exit(1 if fails else 0)
