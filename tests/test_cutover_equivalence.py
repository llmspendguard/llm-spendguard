"""The charges → spend_events CUTOVER equivalence guard — proves the new single money ledger is FAITHFUL to
the old one before any reader/writer is repointed onto it (steps 4→6 of the money-ledger cutover).

Three things, each of which broke money or a cap when it was wrong once:

  A. COUNTABLE EQUIVALENCE — the cap's number is identical old vs new. Seeds `charges` with one row of every
     kind that behaves differently (normal, meta, reconciliation-marker, quarantined-impossible, unpriced,
     negative true-down, reconstructed-basis, a duplicate-cost pair, a genuine zero), migrates, and asserts
     budget.spent_since (countable_charges) == SpendLedger.spent_dec (the _COUNTABLE filter) TO THE CENT, plus
     Σ-conservation (every dollar arrived, incl. the void/quarantine rows) and that each forensic fact survived.

  B. THE FIVE CATEGORIES STAY APART — spent_dec is batch+realtime ONLY; est-value, GPU and the subscription fee
     each read their own column and are NEVER summed into the LLM cap. (Real charges have no est_chat/remote,
     so this is proven on directly-recorded events — the structural guarantee, not an accident of empty columns.)

  C. THE RECORD GUARD — a genuinely missing cost still fails loudly; an EXPLICITLY unpriced $0 row is allowed
     (the call happened, price unknown ≠ free) and is findable by cost_basis.

Offline, isolated home, zero spend.
"""
import os, sys, tempfile
from decimal import Decimal

if not os.environ.get("SPENDGUARD_TEST_ISOLATED"):
    os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"
    os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="spendguard-cutover-")
    os.execv(sys.executable, [sys.executable] + sys.argv)

from spendguard import budget, conv, migrate_charges
from spendguard import ledger as L

fails = []
def ck(name, cond):
    print(("  [OK] " if cond else "  [FAIL] ") + name)
    if not cond:
        fails.append(name)

# isolated home has no taxonomy/transcripts → pin the repo→org map + make resolve hermetic (no real reads)
conv._prior_index = lambda: {"lmm": ("healiom", "lmm"), "manga2anime": ("ensight", "manga2anime")}
conv.segments = lambda *a, **k: []
conv._seg_get_all = lambda: {}

DAY = "2026-06-01"
db = budget._ledger_db()
# charges is the RETIRED source table — budget no longer creates it; this migration test makes its own fixture.
db.execute("CREATE TABLE IF NOT EXISTS charges (ts TEXT, day TEXT, provider TEXT, model TEXT, kind TEXT, "
           "cost REAL, project TEXT DEFAULT '', conv_id TEXT DEFAULT '', key_fp TEXT DEFAULT '', "
           "basis TEXT DEFAULT '', intent TEXT DEFAULT '', actor TEXT DEFAULT '')")
db.commit()


def ins(cost, kind="realtime", model="gpt-5.5", provider="openai", project="lmm", conv_id="c1",
        basis="", intent="", actor="", ts_suffix="00"):
    ts = f"{DAY}T10:00:{ts_suffix}+00:00"
    db.execute("INSERT INTO charges (ts,day,provider,model,kind,cost,project,conv_id,key_fp,basis,intent,actor) "
               "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
               (ts, DAY, provider, model, kind, cost, project, conv_id, "", basis, intent, actor))


# ── SECTION A: seed one of every behaviourally-distinct charge, then migrate ──────────────────────────────
ins(1.50, basis="billed", intent="review:config.py", actor="panel.py:fan_out:238", ts_suffix="01")  # normal realtime, counted
ins(2.00, kind="batch", basis="estimate", ts_suffix="02")                                            # normal batch, counted
ins(0.25, kind="meta", model="claude-opus-4-8", project="llm-spendguard", ts_suffix="03")            # meta → excluded (is_meta)
ins(10.00, kind="batch", model=budget._RECONCILED, basis="billed", ts_suffix="04")                   # reconciliation → excluded (reconciled)
ins(5.00, basis="estimate", conv_id=budget.QUARANTINE_CONV, ts_suffix="05")                          # impossible → excluded (void)
ins(0.00, basis=budget.BASIS_UNPRICED, conv_id=budget.UNPRICED_CONV, ts_suffix="06")                 # unpriced → $0 forensic row
ins(-0.50, kind="batch", conv_id=budget._TRUE_DOWN_CONV, ts_suffix="07")                             # true-down → counted (nets down)
ins(3.00, basis=budget.BASIS_RECONSTRUCTED, ts_suffix="08")                                          # reconstructed → excluded
ins(0.75, conv_id="cP", ts_suffix="09")                                                              # dup-cost pair A → counted
ins(0.75, conv_id="cP", ts_suffix="10")                                                              # dup-cost pair B (diff rowid) → counted
ins(0.00, ts_suffix="11")                                                                            # genuine $0, unmarked → skipped
db.commit()

COUNTABLE = Decimal("1.50") + Decimal("2.00") + Decimal("-0.50") + Decimal("0.75") + Decimal("0.75")  # 4.50
CONSERVED = COUNTABLE + Decimal("0.25") + Decimal("10.00") + Decimal("5.00") + Decimal("3.00")        # 22.75 (all nonzero)

led = L.SpendLedger()
st = migrate_charges.to_spend_events(led=led)
ck("migrated the 10 rows (9 nonzero + 1 unpriced), skipped the 1 genuine zero", st["migrated"] == 10 and st["skipped_zero"] == 1)

# A1 — the cap's number is IDENTICAL old ledger vs new, to the cent
old_countable = budget.spent_since(DAY)
new_countable = Decimal(led.spent_dec(since=DAY))
ck("countable: spend_events spent_dec == exactly $4.50", new_countable == COUNTABLE)
ck("countable: charges spent_since == spend_events spent_dec (to the cent)",
   round(old_countable, 2) == float(COUNTABLE) and round(old_countable, 2) == round(float(new_countable), 2))

# A2 — every dollar arrived (incl. the void/quarantine + reconciled + reconstructed rows)
ck("conservation: Σ all migrated == $22.75 (include_void, include_meta)",
   Decimal(led.sum_dec(include_meta=True, include_void=True)) == CONSERVED)

# A3 — each forensic fact survived the crossing
rows = led.query(where={"source": "migrate:charges"})
by_rt = {L.to_dec(r["realtime_usd"]): r for r in rows if r["realtime_usd"] is not None}
r150 = by_rt.get(Decimal("1.50"))
ck("forensic: cost_basis carries the charge's basis (billed, not the old 'gate')", r150 and r150["cost_basis"] == "billed")
ck("forensic: intent + actor carried across", r150 and r150["intent"] == "review:config.py" and r150["actor"] == "panel.py:fan_out:238")
ck("forensic: quarantined-impossible → status=void (realtime row, kept but voided from totals)",
   any(r["status"] == "void" and L.to_dec(r["realtime_usd"]) == Decimal("5") for r in rows))
ck("forensic: reconciliation marker → reconciled=1", any(r["reconciled"] and L.to_dec(r["batch_usd"]) == Decimal("10") for r in rows))
ck("forensic: reconstructed basis → cost_basis=reconstructed (excluded by _COUNTABLE)",
   any(r["cost_basis"] == "reconstructed" and L.to_dec(r["realtime_usd"]) == Decimal("3") for r in rows))
ck("forensic: true-down survived as a NEGATIVE batch row", any(L.to_dec(r["batch_usd"]) == Decimal("-0.50") for r in rows))
ck("forensic: dup-cost pair BOTH survived (rowid dedup, not value dedup)",
   len([r for r in rows if L.to_dec(r["realtime_usd"]) == Decimal("0.75")]) == 2)
unp = [r for r in rows if r["cost_basis"] == "unpriced"]
ck("forensic: unpriced → 1 row, cost_basis=unpriced, NO money column set",
   len(unp) == 1 and all(unp[0][c] in (None, "") for c in L.USD_COLS))

# A4 — idempotent re-run inserts NO new rows (dedup by source rowid); the countable is unchanged
migrate_charges.to_spend_events(led=L.SpendLedger())
after = L.SpendLedger()
ck("idempotent: re-run leaves exactly 10 migrate:charges rows (no doubling)", len(after.query(where={"source": "migrate:charges"})) == 10)
ck("idempotent: countable unchanged after re-run", Decimal(after.spent_dec(since=DAY)) == COUNTABLE)
ok, _bad = led.verify_audit_chain()
ck("audit hash-chain intact after migration", ok)


# ── SECTION B: the five categories STAY APART (directly-recorded events) ───────────────────────────────────
b = L.SpendLedger(db_path=os.path.join(tempfile.mkdtemp(prefix="sg-cat-"), "cat.db"))
def rec(kind, usd):
    b.record_event({"kind": kind, "usd": usd, "provider": "p", "model": "m", "dedup_key": L.live_dedup_key(kind)})
rec("batch", "3.00"); rec("realtime", "1.00"); rec("est_chat", "9.99"); rec("remote", "7.77"); rec("subscription", "200.00")
ck("split: spent_dec is batch+realtime ONLY == $4.00 (NOT est-value/GPU/sub)", Decimal(b.spent_dec()) == Decimal("4.00"))
ck("split: est_value_dec reads est_chat only == $9.99", Decimal(b.est_value_dec()) == Decimal("9.99"))
ck("split: remote_dec reads remote_compute only == $7.77", Decimal(b.remote_dec()) == Decimal("7.77"))
ck("split: subscription_dec reads subscription only == $200.00", Decimal(b.subscription_dec()) == Decimal("200.00"))
ck("split: sum_dec (reconciliation) is the grand total across all five == $221.76", Decimal(b.sum_dec()) == Decimal("221.76"))


# ── SECTION C: the record guard — missing cost fails, explicit unpriced is allowed ─────────────────────────
raised = False
try:
    b.record_event({"kind": "realtime", "usd": "0", "provider": "p", "model": "m", "dedup_key": L.live_dedup_key("zero")})
except ValueError:
    raised = True
ck("guard: a genuinely $0 (non-unpriced) row still RAISES", raised)
b.record_event({"provider": "p", "model": "m2", "cost_basis": "unpriced", "dedup_key": L.live_dedup_key("unp")})
ck("guard: an explicit unpriced $0 row is ALLOWED and findable by cost_basis",
   len(b.query(where={"cost_basis": "unpriced"})) == 1)

print(("[OK]" if not fails else "[FAIL]") + " cutover-equivalence: %d failure(s)" % len(fails))
sys.exit(1 if fails else 0)
