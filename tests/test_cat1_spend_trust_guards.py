"""Cat-1 spend/trust guards — the NEW honest behaviors from the medium sweep, made un-regressable:

  * crossllm.ask REFUSES (BudgetRefused) when a metered vendor is UNPRICEABLE and a budget_usd is set — an
    unknown price estimates to $0, and a $0 estimate must never sail past a budget and then spend unbounded.
  * trust.cmd exits NON-ZERO (3) when provider billing could not be fetched ('unknown'), so a scheduled/CI run
    never reads a failed verification as success; and trust.trust_report SURFACES a server crosscheck error instead of
    dropping it (an unverified server side is not a healthy one).

Offline, isolated home; no network, no real keys — every price/lane/crosscheck is stubbed in-process.
"""
import os
import sys
import tempfile

if not os.environ.get("SPENDGUARD_TEST_ISOLATED"):
    os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"
    os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="spendguard-cat1-")
    os.execv(sys.executable, [sys.executable] + sys.argv)

from spendguard import crossllm, trust             # noqa: E402
from spendguard.crossllm import BudgetRefused       # noqa: E402

fails = 0
_orig_check = trust.trust_report                            # saved before the loop overwrites it


def ck(label, cond, extra=""):
    global fails
    if not cond:
        fails += 1
    print(f"  [{'OK' if cond else 'FAIL'}] {label}{('  — ' + extra) if extra and not cond else ''}")


# ── crossllm: an UNPRICEABLE metered vendor is refused under a budget (the $0-bypass fix) ─────────────────────
crossllm._is_metered = lambda vendor: True                    # every vendor bills (no $0 lane)
crossllm.pricing.realtime_cost = lambda *a, **k: None         # price UNKNOWN for every model

est, detail, unpriced = crossllm._estimate_metered([("acme", "mystery-1")], "hello", None, 100)
ck("_estimate_metered lists an unpriceable metered vendor in `unpriced`",
   unpriced == ["acme"] and detail == {"acme": None} and est == 0.0, f"got {(est, detail, unpriced)}")

refused = None
try:
    crossllm.ask("hello", vendors=["acme:mystery-1"], budget_usd=0.50)
except BudgetRefused as e:
    refused = e
ck("ask(budget_usd=...) REFUSES when a metered vendor is unpriced (no $0 bypass)", refused is not None)
ck("the refusal names the unpriced vendor + says the price is UNKNOWN",
   refused is not None and refused.unpriced == ["acme"] and "UNKNOWN price" in str(refused))

# a PRICED vendor over budget is refused for the honest 'exceeds' reason (not the unpriced one)
crossllm.pricing.realtime_cost = lambda *a, **k: 9.99
over = None
try:
    crossllm.ask("hello", vendors=["acme:pricey"], budget_usd=0.50)
except BudgetRefused as e:
    over = e
ck("ask(budget_usd=...) refuses a priced-but-over estimate with the 'exceeds' reason",
   over is not None and over.unpriced == [] and "exceeds budget" in str(over))

# a LANE ($0) vendor never counts toward the budget and is never 'unpriced'
crossllm._is_metered = lambda vendor: False
est2, detail2, unpriced2 = crossllm._estimate_metered([("acme", "mystery-1")], "hello", None, 100)
ck("a $0-lane vendor contributes $0 and is never flagged unpriced",
   est2 == 0.0 and detail2 == {"acme": 0.0} and unpriced2 == [])


# ── trust: 'unknown' billing fetch exits non-zero; a server crosscheck error is surfaced ──────────────────────
lvl, _ = trust.verdict(None, 5.0)
ck("verdict(truth=None) is 'unknown', never a silent 'ok'", lvl == "unknown")

# cmd exit codes map the OVERALL level: ok→0, warn→1, alarm→2, unknown→3 (every non-ok is non-zero)
for level, code in {"ok": 0, "warn": 1, "alarm": 2, "unknown": 3}.items():
    trust.trust_report = lambda since=None, _lvl=level: {
        "since": "2026-08-01", "provider_truth": None, "ledger": 0.0,
        "ledger_verdict": {"level": _lvl, "msg": "x"}, "level": _lvl}
    rc = trust.cmd([])
    ck(f"trust.cmd exits {code} when the overall level is '{level}'", rc == code, f"got {rc}")

# check() surfaces a failed server crosscheck as server_error (not dropped), and it elevates an ok ledger to warn
import spendguard.saas as _saas         # noqa: E402
trust.provider_truth = lambda since=None: 10.0
trust._ledger_llm_total = lambda since: 10.0          # ledger ≈ truth → ledger verdict 'ok'
_saas.crosscheck = lambda since=None: {"error": "connection refused"}
out = _orig_check(since="2026-08-01")
ck("check() records a failed server crosscheck as server_error (not dropped)",
   out.get("server_error") == "connection refused" and "server" not in out)
ck("an unverifiable server side elevates an otherwise-ok run to warn", out.get("level") == "warn")

print(f"\n{'[FAIL]' if fails else 'OK'} test_cat1_spend_trust_guards: {fails} failure(s)")
sys.exit(1 if fails else 0)
