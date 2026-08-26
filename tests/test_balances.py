"""Per-vendor metered balance discovery — "how much is available", split sunk-pool vs on-demand, for routing.

The value of the split (auto-top-up is user-declared, not guessed): a SUNK POOL (manual prepay) is money already
spent — idle/expiring credit is pure loss, so it is worth drawing down; an ON-DEMAND vendor (auto-top-up)
recharges from a card, so every dollar is fresh spend and there is no "use it first" incentive. Pins:

  (a) a vendor whose endpoint answers + reads a number → source 'api', kind 'sunk_pool', currency preserved;
  (b) a vendor the operator flagged auto_topup → kind 'on_demand' (same available number, different meaning);
  (c) a vendor with no endpoint / an unreachable fetch → source 'unknown', available None (fail-open, never a 0);
  (d) the agentic field-read is CACHED per raw-hash — an unchanged balance never re-pays for the read;
  (e) a prior-good number is kept (source 'api-stale') when the endpoint is momentarily unreachable.

Offline: the balance GET and the agentic read are stubbed; no network, no key, no spend.
"""
import os
import sys
import tempfile

os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="sg-bal-")
os.environ.setdefault("SPENDGUARD_TEST_ISOLATED", "1")
os.environ.setdefault("SPENDGUARD_NO_AUTOINSTALL", "1")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from spendguard import balances                                                        # noqa: E402

fails = []


def check(name, cond):
    ok = bool(cond)
    print(("  [OK] " if ok else "  [FAIL] ") + name)
    if not ok:
        fails.append(name)


# Control the two heterogeneous vendor shapes + one unreachable vendor, and count agentic reads.
_RAW = {"deepseek": {"balance_infos": [{"currency": "USD", "total_balance": "11.09"}]},
        "moonshot": {"data": {"available_balance": 73.27}}}
_reads = {"n": 0}


def _stub_fetch(vendor, timeout_s=15):
    return _RAW.get(vendor)                       # None for an unlisted/unreachable vendor


def _stub_extract(vendor, raw, run=True):
    _reads["n"] += 1
    return ({"available": 11.09, "currency": "USD", "expiring": None, "note": None} if vendor == "deepseek"
            else {"available": 73.27, "currency": "CNY", "expiring": None, "note": None})


balances.fetch_balance_raw = _stub_fetch
balances.extract_available = _stub_extract
balances._endpoints = lambda: {"deepseek": {"url": "x", "auth": "bearer"},
                               "moonshot": {"url": "x", "auth": "bearer"},
                               "unreachable": {"url": "x", "auth": "bearer"}}


print("-- (a) an endpoint that answers → source 'api', kind 'sunk_pool', currency preserved --")
b = balances.vendor_balance("deepseek", refresh=True)
check("available + currency from the agentic read", b["available"] == 11.09 and b["currency"] == "USD")
check("source 'api', kind 'sunk_pool' (no auto-top-up declared)", b["source"] == "api" and b["kind"] == "sunk_pool")

print("\n-- (b) a vendor the operator flagged auto_topup → 'on_demand' (same number, different meaning) --")
balances._pool_meta = lambda vendor: {"auto_topup": True} if vendor == "deepseek" else {}
b2 = balances.vendor_balance("deepseek", refresh=True)
check("auto_topup → kind 'on_demand', still available", b2["kind"] == "on_demand" and b2["available"] == 11.09 and b2["auto_topup"] is True)
balances._pool_meta = lambda vendor: {}

print("\n-- (c) no endpoint / unreachable fetch → 'unknown', available None (fail-open, never a 0) --")
u = balances.vendor_balance("unreachable", refresh=True)
check("unreachable → source 'unknown', available None (a can't-check is not a zero)", u["source"] == "unknown" and u["available"] is None)
check("...and kind is 'unknown', not a spurious pool", u["kind"] == "unknown")

print("\n-- (d) the agentic read is CACHED per raw-hash — an unchanged balance never re-reads --")
balances.refresh_balances()                       # writes the cache (reads each answering vendor once)
_reads["n"] = 0
again = balances.vendor_balance("deepseek", refresh=True)   # same raw → hash matches → NO new read
check("an unchanged raw balance reuses the cached read ($0, no LLM)", _reads["n"] == 0 and again["available"] == 11.09)

print("\n-- (e) a prior-good number is kept (source 'api-stale') when the endpoint is momentarily unreachable --")
balances.fetch_balance_raw = lambda vendor, timeout_s=15: None    # a REFRESH now finds the endpoint down
stale = balances.vendor_balance("deepseek", refresh=True)         # cache has a prior number to fall back on
check("prior number kept, flagged 'api-stale' (not dropped to unknown)", stale["available"] == 11.09 and stale["source"] == "api-stale")

print(f"\n{'[FAIL]' if fails else 'OK'} test_balances: {len(fails)} failure(s)")
sys.exit(1 if fails else 0)
