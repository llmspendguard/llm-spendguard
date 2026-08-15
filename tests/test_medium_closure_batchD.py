"""Batch-D provider/adapter conformance closure (line-by-line medium fixes):

  * [1]  adapters.provider_for resolves by the LONGEST matching prefix — deterministic regardless of PROVIDERS
    insertion order (an overlapping plugin prefix can't silently route to the wrong vendor).
  * [33] deid maps floor entity names → Presidio labels (EMAIL→EMAIL_ADDRESS…), so an `entities` filter can't
    silently disable NER for those entities.
  * [57/58] modal_adapter.account_total sums the ALREADY-fetched items — no swallowed re-fetch reporting $0.
  * [61/62] provider_kit conformance flags a second activate() that DUPLICATES registrations, not just one that raises.

(Not changed: [63] provider_kit 'priced' — pricing.price() RAISES for an unknown model, so 'resolves' already
means genuinely priced; a numeric threshold would wrongly fail a legitimate free tier.)

Offline, isolated home.
"""
import os
import sys
import tempfile
import datetime

if not os.environ.get("SPENDGUARD_TEST_ISOLATED"):
    os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"
    os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="spendguard-medD-")
    os.execv(sys.executable, [sys.executable] + sys.argv)

from spendguard import adapters, deid, modal_adapter, provider_kit   # noqa: E402

fails = 0


def ck(label, cond, extra=""):
    global fails
    if not cond:
        fails += 1
    print(f"  [{'OK' if cond else 'FAIL'}] {label}{('  — ' + extra) if extra and not cond else ''}")


# ── [1] provider_for: longest prefix wins, order-independent ────────────────────────────────────────────────────
try:
    adapters.PROVIDERS["zz_short"] = {"prefixes": ("qx",)}
    adapters.PROVIDERS["zz_long"] = {"prefixes": ("qx-pro",)}
    ck("longest (most specific) prefix wins", adapters.provider_for("qx-pro-7") == "zz_long")
    ck("a shorter-only match still resolves", adapters.provider_for("qxmini") == "zz_short")
    del adapters.PROVIDERS["zz_short"], adapters.PROVIDERS["zz_long"]
    adapters.PROVIDERS["zz_long"] = {"prefixes": ("qx-pro",)}          # reversed insertion order
    adapters.PROVIDERS["zz_short"] = {"prefixes": ("qx",)}
    ck("resolution is independent of PROVIDERS insertion order", adapters.provider_for("qx-pro-7") == "zz_long")
finally:
    adapters.PROVIDERS.pop("zz_short", None)
    adapters.PROVIDERS.pop("zz_long", None)

# ── [33] deid floor→Presidio entity mapping ─────────────────────────────────────────────────────────────────────
ck("floor EMAIL maps to Presidio EMAIL_ADDRESS", deid._PRESIDIO_ENTITY.get("EMAIL") == "EMAIL_ADDRESS")
mapped = [deid._PRESIDIO_ENTITY.get(n, n) for n in ["EMAIL", "PHONE", "PERSON"]]
ck("floor names map, native Presidio names pass through", mapped == ["EMAIL_ADDRESS", "PHONE_NUMBER", "PERSON"])


# ── [57/58] modal account_total sums already-fetched items (no swallowed re-fetch) ──────────────────────────────
class _It:
    def __init__(self, cost):
        self.cost = cost
        self.interval_start = datetime.datetime(2026, 8, 1, tzinfo=datetime.timezone.utc)
        self.object_id, self.description, self.environment_name = "o", "d", "e"


_calls = {"n": 0}


def _flaky(since_ts):
    _calls["n"] += 1
    if _calls["n"] > 1:
        raise RuntimeError("second fetch fails")     # the re-fetch the OLD code did would land here → []→$0
    return [_It(2.0), _It(3.0)]


modal_adapter._report = _flaky
t = modal_adapter.PROVIDER.account_total(since=None)
ck("account_total sums the fetched items, no swallowed second fetch", t == 5.0 and _calls["n"] == 1, f"t={t} n={_calls['n']}")

# ── [61/62] provider_kit flags a duplicating (non-idempotent) activate ──────────────────────────────────────────
_reg = {"n": 0}


def _dup_activate():
    _reg["n"] += 1
    adapters.PROVIDERS[f"dupe_{_reg['n']}"] = {"prefixes": ("zzzq",)}   # grows the set each call → non-idempotent


try:
    results = provider_kit.run_conformance(_dup_activate, name="dupe_1", kind="adapter")
    idem = [r for r in results if r[0] == "idempotent"][0]
    ck("conformance flags a non-idempotent (duplicating) activate", idem[1] is False, f"got {idem}")
finally:
    for k in [k for k in list(adapters.PROVIDERS) if k.startswith("dupe_")]:
        adapters.PROVIDERS.pop(k, None)

print(f"\n{'[FAIL]' if fails else 'OK'} test_medium_closure_batchD: {fails} failure(s)")
sys.exit(1 if fails else 0)
