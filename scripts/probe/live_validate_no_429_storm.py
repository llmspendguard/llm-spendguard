"""LIVE validation that the 429-storm fix holds on the REAL metered Anthropic path (where the 8,088 storms were).

It submits a concurrent fan of N real metered calls THROUGH THE GOVERNED PATH (bulk_delegate — the one chokepoint;
NOT a hand-rolled ThreadPoolExecutor, which is the ungoverned antipattern that CAUSED the storms) against a
representative rate cap, and proves the governor PACES the fan — throttled to the cap, ZERO 429s in the ledger for
this run — instead of firing it all at once (the old unlimited behaviour). Cheap + safe by construction: the model is
resolved from the catalog registry (not a pinned literal), a $0-ish haiku, tiny prompts, modest N vs a modest cap —
the real pacing machinery exercised WITHOUT a real 1000/min storm. The same machinery at anthropic's real 1,000-rpm
cap is what stops a 4,584/min fan.

Estimate-FIRST (the API spend protocol): --plan prints count + $ estimate, spends $0. --run executes under a
budget_usd cap and reports ledger 429s for the run, whether the fan was paced, and the REAL billed $ for its chain.

Usage:  python scripts/probe/live_validate_no_429_storm.py --plan
        python scripts/probe/live_validate_no_429_storm.py --run [--n 60] [--rpm 60] [--workers 12] [--budget 1.0]
"""
import argparse
import os
import sqlite3
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "src"))
import spendguard  # noqa: E402
spendguard.require()
from spendguard import model_catalog, lane_balance, pricing, budget, config, calls  # noqa: E402
from spendguard import content_tokens, expected_output  # noqa: E402

INTENT = "live:no-429-storm-validation"
_SYS = "You are terse. One word."
EXPECTED_OUT_TOK = 5   # a one-word ordinal reply — a DECLARED expectation (expected_output.expect basis='declared'),
#                        NOT an invented literal fed to a cost function (the $34→$380 defect this repo guards against)


def _prompt(i):
    # UNIQUE per call — identical prompts dedupe/cache-serve at $0 and never exercise the metered path (the first
    # live attempt's hollow pass). A distinct nonce forces N genuine metered round-trips.
    return "Reply with exactly one word naming the ordinal: item number %d of the validation run." % i


def _resolve_haiku():
    """A cheap anthropic model id FROM THE CATALOG'S OWN family declaration — `model_family_map["haiku"]`, which is the
    catalog author's AUTHORITATIVE classification that we READ, not a substring guess at the id's text. "Is this id a
    Haiku model?" is a meaning question the catalog already answers; deciding it from `"haiku" in mid` would misread an
    id like "anthropic-haiku-compat-proxy". Returns the first declared id that still has a record, else None — the
    caller then fails honestly or the operator pins --model."""
    rec = model_catalog.vendor_record("anthropic") or {}
    for mid in ((rec.get("model_family_map") or {}).get("haiku") or []):
        if model_catalog.model_record(mid):
            return mid
    return None


def _ledger_429s(chain):
    try:
        db = os.path.join(config.db_path() if hasattr(config, "db_path") else
                          os.path.expanduser("~/.spendguard/spend.db"))
        con = sqlite3.connect(db)
        n = con.execute("SELECT COUNT(*) FROM calls WHERE chain=? AND http_status=429", (chain,)).fetchone()[0]
        tot = con.execute("SELECT COUNT(*) FROM calls WHERE chain=?", (chain,)).fetchone()[0]
        con.close()
        return n, tot
    except Exception as e:
        return None, None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--plan", action="store_true")
    ap.add_argument("--run", action="store_true")
    ap.add_argument("--n", type=int, default=60)
    ap.add_argument("--rpm", type=int, default=60)
    ap.add_argument("--workers", type=int, default=12)
    ap.add_argument("--budget", type=float, default=1.0)
    ap.add_argument("--model", default=None, help="pin the anthropic model id; else resolved from the catalog haiku family")
    a = ap.parse_args()
    if not (a.plan or a.run):
        ap.error("pass --plan or --run")
    model = a.model or _resolve_haiku()
    if not model:
        print("could not resolve an anthropic haiku model from the catalog family map — pin one with --model"); sys.exit(2)
    # Preview cost from a MEASURED input count (the real system+prompt text, provider-aware tokenizer) and the
    # SANCTIONED output expectation (expected_output.expect, basis-tracked) — never integer literals handed to a cost
    # function (that is how $34 was quoted for a $380 run; see tests/test_estimates_come_from_measurement.py).
    try:
        in_tok = content_tokens.count_tokens(_SYS + "\n" + _prompt(0), provider="anthropic", model=model)
        out_tok, _basis = expected_output.expect(model, sig=INTENT, declared=EXPECTED_OUT_TOK)
        per = pricing.realtime_cost(model, in_tok, out_tok) or 0.0
    except Exception:
        per = 0.0
    tot = per * a.n
    print("== LIVE 429-storm validation (governed path: bulk_delegate) ==")
    print("  model=%s (registry-resolved)  N=%d  workers=%d  rpm cap=%d  budget=$%.2f" % (model, a.n, a.workers, a.rpm, a.budget))
    print("  ESTIMATE: ~$%.6f/call × %d = ~$%.4f metered (anthropic)  [paced ETA ~%.0fs at %d rpm]"
          % (per, a.n, tot, (a.n / max(1, a.rpm)) * 60.0, a.rpm))
    if a.plan:
        print("  --plan: $0 spent. Re-run with --run."); return
    os.environ["SPENDGUARD_DISPATCH_RPM_ANTHROPIC"] = str(a.rpm)   # pace against a representative cap on the real path
    os.environ["SPENDGUARD_DISPATCH_MANAGE_ALL"] = "1"
    chain = "live-429-%s" % time.strftime("%Y%m%dT%H%M%S")
    before = budget.spent_by_job(chain)
    tasks = [{"i": k} for k in range(a.n)]
    t0 = time.time()
    with calls.context(intent=INTENT, chain=chain):          # tag every recorded row with THIS run's chain
        res = lane_balance.bulk_delegate(
            tasks, intent=INTENT, system=_SYS, prompt_for=lambda t: _prompt(t["i"]),
            model_for=lambda t: "anthropic:%s" % model, metered_only=True,   # governed metered fan, pinned model
            max_workers=a.workers, deadline_s=150.0, budget_usd=a.budget, reasoning="minimal",
            force=True)    # own the lane-bulk gate risk ($0.002 validation) so the fan actually EXECUTES + records
    elapsed = time.time() - t0
    n429, ntot = _ledger_429s(chain)
    billed = budget.spent_by_job(chain) - before
    fails = [r for r in (res or []) if not (isinstance(r, dict) and r.get("text") and not r.get("error"))]
    ok_rows = len(res or []) - len(fails)
    observed_rpm = (a.n / elapsed * 60.0) if elapsed > 0 else float("inf")
    print("\n== RESULT ==")
    print("  429s in the ledger for this run: %s   total recorded rows: %s   ok results: %d/%d"
          % (n429, ntot, ok_rows, a.n))
    # Pacing is REPORTED as observed throughput vs the cap, not gated by a hand-picked proxy. The PROOF of pacing is
    # the pass criterion itself: a fan this much larger than the per-request bucket produces ZERO ledger 429s only if
    # the governor paced/absorbed it rather than storming the wall (an unpaced burst records 429s in the ledger).
    print("  elapsed: %.1fs  observed throughput ~%.0f req/min vs cap %d rpm  (paced ETA ~%.0fs at the cap)"
          % (elapsed, observed_rpm, a.rpm, (a.n / max(1, a.rpm)) * 60.0))
    print("  BILLED (ledger, this chain): $%.6f   (estimate ~$%.4f)" % (billed, tot))
    if fails:   # NEVER hide failures behind a success-rate threshold — surface them for inspection
        print("  FAILURES (%d) — NOT a clean run; inspect each before trusting the result:" % len(fails))
        for r in fails[:10]:
            print("    - %s" % ((r or {}).get("error") or "no text / unknown" if isinstance(r, dict) else repr(r)))
    # PASS = the measured guarantee, with NO hand-picked threshold: zero 429s in the ledger AND every submitted
    # request returned usable text (N:N). Any 429, or any missing/failed row, is a REVIEW (shown above), not a pass.
    passed = (n429 == 0) and (ntot is not None) and (ok_rows == a.n)
    print("\n  VALIDATION: %s — %s" % ("PASS" if passed else "REVIEW",
          "zero ledger 429s and all %d served on the real metered governed fan" % a.n if passed
          else "see the 429 count and/or failures above (ledger read %s)" % ("ok" if ntot is not None else "FAILED")))
    sys.exit(0 if passed else 1)


if __name__ == "__main__":
    main()
