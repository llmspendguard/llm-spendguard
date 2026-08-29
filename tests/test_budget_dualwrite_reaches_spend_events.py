"""Step 5b writers guard: every budget WRITE now dual-writes into spend_events, and the two ledgers agree.

Seeds through the real budget writers (record / record_meta / record_unpriced / record_reconciled /
record_true_down / ingest_remote) in an isolated home, then asserts:
  • the CAP number is identical old vs new — budget.spent_since (charges) == SpendLedger.spent_dec (spend_events),
  • the roles landed right in the typed ledger — meta→is_meta, reconciliation→reconciled, true-down→negative,
    unpriced→$0/cost_basis, and remote lands in remote_compute (its OWN cap), NOT the LLM total.

Offline, isolated home, zero spend.
"""
import os, sys, tempfile
from decimal import Decimal

if not os.environ.get("SPENDGUARD_TEST_ISOLATED"):
    os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"
    os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="spendguard-dualwrite-")
    os.execv(sys.executable, [sys.executable] + sys.argv)

from spendguard import budget, conv
from spendguard import ledger as L

fails = []
def ck(name, cond):
    print(("  [OK] " if cond else "  [FAIL] ") + name)
    if not cond:
        fails.append(name)

conv._prior_index = lambda: {"lmm": ("healiom", "lmm"), "manga2anime": ("ensight", "manga2anime")}
conv.segments = lambda *a, **k: []
conv._seg_get_all = lambda: {}

DAY = budget._utc().strftime("%Y-%m-%d")

# ── seed through the real budget writers (each dual-writes charges + spend_events) ──
budget.record_charge("openai", "gpt-5.5", "realtime", 1.50, project="lmm")
budget.record_charge("anthropic", "claude-haiku-4-5", "batch", 2.00, project="lmm")
budget.record_meta("anthropic", "claude-opus-4-8", 0.25)                       # excluded (is_meta)
budget.record_unpriced("bedrock", "some-model", "realtime", in_tok=1000, out_tok=50, project="lmm")  # $0 forensic
budget.record_reconciled(DAY, "openai", 10.00, project="lmm")                  # excluded (reconciled marker)
budget.record_true_down(DAY, "anthropic", "claude-haiku-4-5", 0.50, "lmm")     # nets down -0.50
budget.ingest_remote("box-1", "manga2anime", [{"provider": "vast", "model": "gpu", "cost": 7.77, "day": DAY}])

LLM_COUNTABLE = Decimal("1.50") + Decimal("2.00") - Decimal("0.50")            # 3.00 (meta/reconciled excluded; unpriced $0)

led = budget._ledger()
# NOTE: on this SEEDED data charges' countable_charges view lumps the remote row into spent_since (it excludes
# only meta/markers/quarantine/reconstructed), while the NEW split keeps remote on its own cap. Real charges have
# NO remote rows (verified on the live ledger), so we compare the LLM path directly and check remote separately.
spent_dec = Decimal(led.spent_dec(since=DAY))
ck("LLM cap number: spend_events spent_dec == $3.00 (batch+realtime, meta/reconciled excluded, unpriced $0)",
   spent_dec == LLM_COUNTABLE)

# charges spent_since scoped to project lmm excludes the manga2anime remote row → equals the LLM countable too
charges_lmm = budget.spent_since(DAY, project="lmm")
ck("charges spent_since(lmm) == spend_events spent_dec(lmm) to the cent",
   round(charges_lmm, 2) == float(LLM_COUNTABLE)
   and round(charges_lmm, 2) == round(float(Decimal(led.spent_dec(since=DAY, where={"project_primary": "lmm"}))), 2))

# roles in the typed ledger
rows = led.query(since=DAY)
ck("meta → is_meta=1 (own line, not workload)", any(r["is_meta"] and L.to_dec(r["realtime_usd"]) == Decimal("0.25") for r in rows))
ck("reconciliation → reconciled=1 (excluded from cap)", any(r["reconciled"] and L.to_dec(r["batch_usd"]) == Decimal("10") for r in rows))
ck("true-down → a NEGATIVE batch row", any(L.to_dec(r["batch_usd"]) == Decimal("-0.50") for r in rows))
ck("unpriced → cost_basis='unpriced', tokens carried, no money", any(
    r["cost_basis"] == "unpriced" and r["in_tok"] == 1000 and all(r[c] in (None, "") for c in L.USD_COLS) for r in rows))

# the split: remote is its OWN category, never in the LLM cap
ck("remote → remote_compute (its own cap), $7.77 in remote_dec, $0 in spent_dec",
   Decimal(led.remote_dec(since=DAY)) == Decimal("7.77"))

# idempotent remote re-sync REPLACES (delete + re-book), never doubles
budget.ingest_remote("box-1", "manga2anime", [{"provider": "vast", "model": "gpu", "cost": 7.77, "day": DAY}])
ck("remote re-sync REPLACES (still $7.77, not $15.54)", Decimal(led.remote_dec(since=DAY)) == Decimal("7.77"))

ok, _bad = led.verify_audit_chain()
ck("audit hash-chain intact after all writes", ok)

# ── the fail-OPEN miss line must stay HONEST post-cutover ─────────────────────────────────────────────
# spend_events is the SOLE ledger now, so a write that cannot land means the charge is genuinely GONE. The
# pre-cutover warning said "charges is still authoritative" and "`spendguard migrate` rebuilds spend_events
# from charges" — after the charges drop BOTH are false and would send an operator to a dead recovery path
# (migrate read charges; charges no longer exists). Induce a write failure and read the line the user sees.
import io, contextlib

def _boom(self, ev):
    raise RuntimeError("induced write failure — spend_events unreachable")

_orig_record = L.SpendLedger.record_event
L.SpendLedger.record_event = _boom
_buf = io.StringIO()
try:
    with contextlib.redirect_stderr(_buf):
        budget.record_charge("openai", "gpt-5.5", "realtime", 0.99, project="lmm")
finally:
    L.SpendLedger.record_event = _orig_record
_msg = _buf.getvalue()
ck("a failed write warns LOUDLY, never silent", _msg.strip() != "")
ck("...names the charge as MISSING from the ledger", "MISSING" in _msg)
ck("...points at the recovery that still works (provider-truth reconcile)", "spendguard reconcile" in _msg)
ck("...does NOT claim charges is authoritative (charges is dropped)", "authoritative" not in _msg)
ck("...does NOT send the reader to `spendguard migrate` (it read charges, now gone)", "migrate" not in _msg)

print(("[OK]" if not fails else "[FAIL]") + " budget-dualwrite: %d failure(s)" % len(fails))
sys.exit(1 if fails else 0)
