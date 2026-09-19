"""GUARD — budget.reprice_unpriced retroactively PRICES 'unpriced' ledger rows once a rate resolves, through the
ledger's OWN audited update()/adjust() (never raw SQL). An 'unpriced' row is a forensic marker: the call happened
(tokens real) but its $ was unknown, so it stays OUT of every total and shows in the 'cannot price' view. When the
model becomes priceable it must FOLD INTO the total AND drop out of that view; a row STILL unpriceable is LEFT
unpriced — never a guessed number. Dry-run changes nothing.

Hermetic: isolated home, a model priced in-test vs one left unpriceable; no network, no real ledger."""
import os
import sys
import tempfile

os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="spendguard-reprice-")
os.environ.setdefault("SPENDGUARD_TEST_ISOLATED", "1")
os.environ.setdefault("SPENDGUARD_NO_AUTOINSTALL", "1")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from spendguard import budget, pricing   # noqa: E402

fails = []


def ck(name, cond):
    print(("  [OK] " if cond else "  [FAIL] ") + name)
    if not cond:
        fails.append(name)


PRICED = "zzz-reprice-priceable"      # we give this one a rate mid-test
UNPRICED = "zzz-reprice-no-price"     # this one stays unpriceable → must be left alone


def _unpriced_models():
    return {r["model"] for r in budget.unpriced_since("1970-01-01")}


# both recorded UNPRICED (real tokens, unknown $)
budget.record_unpriced("zai", PRICED, "realtime", in_tok=1_000_000, out_tok=1_000_000)
budget.record_unpriced("zai", UNPRICED, "realtime", in_tok=500_000, out_tok=0)
ck("both models start in the cannot-price view", {PRICED, UNPRICED} <= _unpriced_models())

# Deterministic pricing (hermetic — no dependence on the synced price table): PRICED resolves to $1/1M in + $2/1M
# out; UNPRICED raises like a genuinely-unpriceable model. reprice_unpriced re-imports pricing, so it sees these.
def _stub_rc(m, in_t, out_t=0, provider=None, **k):
    if m == PRICED:
        return in_t / 1e6 * 1.0 + out_t / 1e6 * 2.0
    raise KeyError(m)


def _stub_price(m, provider=None):
    if m == PRICED:
        return {"in_": 1.0, "out": 2.0}
    raise KeyError(m)


pricing.realtime_cost = _stub_rc
pricing.price = _stub_price

print("\n-- DRY-RUN plans the reprice but changes nothing --")
plan = budget.reprice_unpriced(PRICED, apply=False)
upd = [p for p in plan if p["method"] == "update"]
ck("dry-run plans exactly one in-place update for the now-priceable model", len(upd) == 1)
ck("priced at the CATALOG rate: 1M in @ $1 + 1M out @ $2 = $3.00", upd and abs(upd[0]["cost"] - 3.0) < 1e-6)
ck("dry-run did NOT touch the ledger (still unpriced)", PRICED in _unpriced_models())
_eid = upd[0]["id"] if upd else None

print("\n-- APPLY: the now-priceable rows fold in; the still-unpriceable one is left alone --")
budget.reprice_unpriced(PRICED, apply=True)
row = budget._ledger().get(_eid) or {}
ck("the row is now cost_basis='estimate' with the money booked (folds into the total)",
   row.get("cost_basis") == "estimate" and float(row.get("realtime_usd") or 0) > 0)
ck("the priceable model DROPS OUT of the cannot-price view", PRICED not in _unpriced_models())
ck("the still-unpriceable model is LEFT unpriced (never a guessed number)", UNPRICED in _unpriced_models())

print(f"\n{'[FAIL]' if fails else 'OK'} test_reprice_unpriced: {len(fails)} failure(s)")
sys.exit(1 if fails else 0)
