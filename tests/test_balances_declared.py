"""Declared-minus-spend prepay ledger for vendors with NO balance API (openai/anthropic/gemini expose none with a
plain key). Fills the 'unknown' gap balances.py leaves for them, so routing can know how much is available on EVERY
provider (real API for deepseek/moonshot, declared-minus-spend here). Pins:
  (a) _apply_auto_reload — a plain sunk drawdown; a reload that tops up to `to` each time it crosses `trigger`
      (result stays in [trigger, to]); a malformed reload degrades to a plain drawdown;
  (b) declared_balance — declared - spend; auto_reload vs sunk_pool kind; the `reloading` flag; and the FAIL-SAFE:
      a spend-ledger that can't be read yields available=None/source 'unknown' (NEVER the full declared amount —
      overstating available is the unsafe direction).
Offline: _spend_since / config stubbed; no ledger, no network.
"""
import os
import sys
import tempfile

os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="sg-baldecl-")
os.environ.setdefault("SPENDGUARD_TEST_ISOLATED", "1")
os.environ.setdefault("SPENDGUARD_NO_AUTOINSTALL", "1")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from spendguard import balances                                                         # noqa: E402

fails = []


def ck(name, cond):
    ok = bool(cond)
    print(("  [OK] " if ok else "  [FAIL] ") + name)
    if not ok:
        fails.append(name)


print("-- (a) _apply_auto_reload: sunk drawdown, reload tops up across the trigger, stays in [trigger, to] --")
ck("no reload (to<=trigger) → plain declared-minus-spend", balances._apply_auto_reload(100, 30, None, None) == 70)
ck("spend within the pre-first-reload band → simple drawdown", balances._apply_auto_reload(14.78, 3, 10, 15) == 11.78)
# openai's real shape: declared 14.78, trigger 10, to 15, spent 29.70 → 5 reloads, lands at 10.08
r = balances._apply_auto_reload(14.78, 29.70, 10, 15)
ck("crossing the trigger reloads to `to` (openai case) → ~10.08", abs(r - 10.08) < 1e-6)
ck("...and the result is within [trigger, to]", 10 <= r <= 15)
ck("a malformed reload (to<=trigger) degrades to a plain drawdown, floored at 0", balances._apply_auto_reload(5, 20, 10, 8) == 0.0)

print("\n-- (b) declared_balance: declared - spend, kind, reloading, and the fail-safe on an unreadable ledger --")
_o_decl, _o_spend = balances._declared, balances._spend_since
try:
    # an AUTO-RELOAD (on_demand) vendor
    balances._declared = lambda v: {"amount": 14.78, "currency": "USD", "as_of": "2026-08-27",
                                    "auto_reload": {"trigger": 10, "to": 15}, "monthly_cap": 1000} if v == "openai" else {}
    balances._spend_since = lambda v, a: 29.70
    b = balances.declared_balance("openai")
    ck("available = the auto-reload result (~10.08)", abs(b["available"] - 10.08) < 1e-3)
    ck("kind on_demand (auto_reload declared)", b["kind"] == "on_demand" and b["auto_topup"] is True)
    ck("source 'declared'", b["source"] == "declared")
    ck("reloading=True (spend crossed the trigger)", b["reloading"] is True)
    ck("spent_since is surfaced", abs(b["spent_since"] - 29.70) < 1e-6)

    # a SUNK pool (no auto_reload) — gemini credit
    balances._declared = lambda v: {"amount": 125.0, "currency": "USD", "as_of": "2026-08-27", "payg": True} if v == "gemini" else {}
    balances._spend_since = lambda v, a: 0.08
    g = balances.declared_balance("gemini")
    ck("sunk pool available = declared - spend", abs(g["available"] - 124.92) < 1e-6)
    ck("kind sunk_pool (no auto_reload), not reloading", g["kind"] == "sunk_pool" and g["reloading"] is False)

    # nothing declared → UNKNOWN (never a zero)
    balances._declared = lambda v: {}
    u = balances.declared_balance("openai")
    ck("no declared balance → available None, source 'unknown' (not a fabricated 0)", u["available"] is None and u["source"] == "unknown")

    # THE FAIL-SAFE: spend ledger unreadable → UNKNOWN, never the full declared amount
    balances._declared = lambda v: {"amount": 100.0, "currency": "USD", "as_of": "2026-06-01"}
    balances._spend_since = lambda v, a: None                # ledger read failed
    f = balances.declared_balance("openai")
    ck("unreadable spend ledger → available None / 'unknown' (NOT the full $100 — overstating is the unsafe way)",
       f["available"] is None and f["source"] == "unknown")
finally:
    balances._declared, balances._spend_since = _o_decl, _o_spend

print(f"\n{'[FAIL]' if fails else 'OK'} test_balances_declared: {len(fails)} failure(s)")
sys.exit(1 if fails else 0)
